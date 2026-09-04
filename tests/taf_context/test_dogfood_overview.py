"""Opt-in end-to-end check of repository-overview against one pinned commit.

The fixture pins the directory groups this repository has at the commit named
in it, so the measurement is reproducible: a clone is checked out at that
commit, indexed, and asked for its overview. Every row of the fixture was
re-derived without the engine (`git ls-files` for the files and the languages,
CPython's `ast` and `go/ast` for the definitions, the well-known entry-name
rule for the entry points), which is what makes the comparison a check rather
than a recording of whatever the engine happened to answer.

The first test asserts the whole group table, the summary and the ordering
invariants of the file layer; the second that the same request answers byte
for byte the same way twice; the third that a `--path-prefix` re-roots the
answer on the children of that subtree and re-derives the same counts one
level down; the fourth that asking for several prefixes is served from the
first in sorted order and says so; the fifth that the output budget is what
sizes the table, folding its tail into the `"*"` row around a reserved file
layer, so a wider budget buys a wider table.
"""

from __future__ import annotations

from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from taf_context.cli import main

ROOT = Path(__file__).parents[2]
FIXTURE = ROOT / "tools" / "taf-context-native" / "testdata" / "dogfood" / "overview.json"
GROUP_KEYS = (
    "path_prefix",
    "depth",
    "file_count",
    "definition_count",
    "entry_point_count",
    "document_count",
    "configuration_count",
    "languages",
)
FOLDED_PREFIX = "*"


@unittest.skipUnless(
    os.environ.get("TAF_DOGFOOD") == "1" and os.environ.get("TAF_LEVEL1_BINARY"),
    "set TAF_DOGFOOD=1 and TAF_LEVEL1_BINARY to run the broker dogfood",
)
class DogfoodOverviewTests(unittest.TestCase):
    fixture: dict
    environment: dict[str, str]
    repository: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls._directory = tempfile.TemporaryDirectory()
        directory = Path(cls._directory.name)
        cls.repository = directory / "repo"
        cls.environment = {
            "HOME": str(directory),
            "PATH": os.environ.get("PATH", ""),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TAF_LEVEL1_BINARY": os.environ["TAF_LEVEL1_BINARY"],
            "TAF_STATE_HOME": str(directory / "state"),
        }
        commit = cls.fixture["commit"]
        try:
            subprocess.run(
                ["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(cls.repository)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(cls.repository), "checkout", "-q", "--detach", commit],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as error:
            cls._directory.cleanup()
            # A checkout that cannot reach the pinned commit says nothing about
            # the operation, so it skips instead of failing.
            raise unittest.SkipTest(
                f"the pinned commit {commit} is unreachable from this checkout: "
                f"{error.stderr.decode('utf-8', 'replace').strip()}"
            )
        cls._cache: dict[tuple[str, ...], tuple[str, dict[str, object]]] = {}
        built = cls._invoke(
            "prepare", "build", "--repo", str(cls.repository), "--confirm-state-write"
        )[1]
        if built["next_safe_action"] != "use-index":
            cls._directory.cleanup()
            raise unittest.SkipTest("the clone of the pinned commit did not become ready")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._directory.cleanup()

    @classmethod
    def _invoke(cls, *argv: str) -> tuple[str, dict[str, object]]:
        stdout, stderr = StringIO(), StringIO()
        code = main(list(argv), stdout=stdout, stderr=stderr, environment=cls.environment)
        if (code, stderr.getvalue()) != (0, ""):
            # A bare `assert` is removed under `python -O`, which would let a
            # non-zero exit code surface later as an opaque JSONDecodeError.
            raise AssertionError(f"CLI call {argv!r} exited {code}: {stderr.getvalue()!r}")
        text = stdout.getvalue()
        return text, json.loads(text)

    @classmethod
    def _overview(
        cls, *prefixes: str, cached: bool = True, budget: int | None = None
    ) -> tuple[str, dict[str, object]]:
        # The stages of the tests below ask for the same few answers, so one
        # engine call per prefix tuple and budget is enough; `cached=False` is
        # what the determinism test uses to ask for a genuinely second call.
        request = cls.fixture["request"]
        if budget is None:
            budget = int(request["maximum_output_characters"])
        argv = [
            "prepare",
            "query",
            "--repo",
            str(cls.repository),
            "--operation",
            "repository-overview",
        ]
        for prefix in prefixes:
            argv += ["--path-prefix", prefix]
        argv += [
            "--maximum-results",
            str(request["maximum_results"]),
            "--maximum-output-characters",
            str(budget),
        ]
        if not cached:
            return cls._invoke(*argv)
        key = prefixes + (str(budget),)
        if key not in cls._cache:
            cls._cache[key] = cls._invoke(*argv)
        return cls._cache[key]

    def _table(self, result: dict[str, object]) -> list[dict[str, object]]:
        return [{key: group[key] for key in GROUP_KEYS} for group in result["groups"]]

    def _group_of(self, path: str, prefixes: list[str]) -> str:
        # The group a file belongs to is the longest kept prefix that holds it:
        # a `<dir>/.` row holds the files directly inside `<dir>`, the `"."` row
        # the files at the repository root, and any other row its whole subtree.
        best = ""
        for prefix in prefixes:
            if prefix == FOLDED_PREFIX:
                continue
            if prefix == ".":
                holds = "/" not in path
            elif prefix.endswith("/."):
                directory = prefix[:-1]
                holds = path.startswith(directory) and "/" not in path[len(directory):]
            else:
                holds = path.startswith(prefix)
            if holds and len(prefix) > len(best):
                best = prefix
        return best

    def test_the_group_table_matches_the_hand_checked_directories(self) -> None:
        expected = self.fixture["repository"]
        _text, result = self._overview()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["freshness"], "exact")
        self.assertEqual(result["next_safe_action"], "use-index")
        self.assertNotIn("overview-root-first-prefix", result["warnings"])
        self.assertNotIn("output-budget-exceeded", result["warnings"])
        self.assertEqual(
            result["overview"],
            {
                "root": expected["root"],
                "counted_file_count": expected["counted_file_count"],
                "other_group_count": expected["other_group_count"],
            },
        )
        # The whole table, row by row and count by count, in the order the
        # operation ranks the groups.
        self.assertEqual(self._table(result), expected["groups"])
        # The file layer is bounded by the output budget, not by the request:
        # what it must report honestly is how much of the repository it left
        # out and that it did leave something out.
        self.assertTrue(result["truncated"])
        self.assertEqual(
            result["omitted_count"], expected["counted_file_count"] - result["returned_count"]
        )
        self.assertLessEqual(
            result["output_characters"], self.fixture["request"]["maximum_output_characters"]
        )

        prefixes = [group["path_prefix"] for group in result["groups"]]
        paths = [finding["path"] for finding in result["findings"]]
        tier0 = set(expected["tier0_paths"])
        # The first round is global: every entry point of the repository leads
        # the layer, whichever group holds it, so where the repository starts
        # is not buried behind the first file of each larger directory.
        self.assertEqual(set(paths[: len(tier0)]), tier0)
        # The round-robin follows it and names every group once, in table
        # order, at the file the group ranks first — except where the global
        # round already took that file, which is where a group's second file
        # stands in for it.
        rest = paths[len(tier0):]
        for position, path in enumerate(expected["first_findings"]):
            if path in tier0:
                continue
            self.assertEqual(rest[position], path, prefixes[position])
        identities = {finding["result_identity"]: finding["path"] for finding in result["findings"]}
        for group, path in zip(result["groups"], expected["first_findings"]):
            # A group is represented by the first file it ranks, so its
            # representative identity is that file's finding.
            self.assertEqual(identities.get(group["representative_identity"]), path, group)
            self.assertEqual(self._group_of(path, prefixes), group["path_prefix"], path)

        # Tier 0 is the entry points and the well-known entry names; each one
        # must rank ahead of every ordinary file, of its own group and of any
        # other, which is the global first round seen from the file layer.
        self.assertTrue(tier0.issubset(set(paths)), sorted(tier0 - set(paths)))
        seen: dict[str, str] = {}
        for path in paths:
            group = self._group_of(path, prefixes)
            if path in tier0:
                self.assertNotIn(group, seen, (path, seen.get(group)))
            elif group not in seen:
                seen[group] = path

    def test_the_same_request_answers_the_same_way_twice(self) -> None:
        first, _result = self._overview()
        again, _repeated = self._overview(cached=False)
        # Byte-identical, not merely equal once parsed: the answer carries no
        # timing, no ordering that depends on a map walk, and no state that a
        # second query over the same index could change.
        self.assertEqual(first, again)

    def test_a_subtree_overview_describes_the_children_of_the_prefix(self) -> None:
        expected = self.fixture["subtree"]
        _text, result = self._overview(expected["path_prefix"])
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("overview-root-first-prefix", result["warnings"])
        self.assertEqual(
            result["overview"],
            {
                "root": expected["root"],
                "counted_file_count": expected["counted_file_count"],
                "other_group_count": expected["other_group_count"],
            },
        )
        self.assertEqual(self._table(result), expected["groups"])
        # This budget holds the whole subtree table, so every row names a real
        # directory of it: nothing was folded and nothing is missing.
        self.assertNotIn(
            FOLDED_PREFIX, [group["path_prefix"] for group in result["groups"]]
        )
        self.assertEqual(
            sum(group["file_count"] for group in result["groups"]),
            expected["counted_file_count"],
        )
        for group in result["groups"]:
            self.assertTrue(group["path_prefix"].startswith(expected["root"]), group)
        for finding in result["findings"]:
            self.assertTrue(finding["path"].startswith(expected["root"]), finding["path"])

    def test_several_prefixes_are_served_from_the_first_in_sorted_order(self) -> None:
        # The wire caps the number of prefixes but does not forbid several, so
        # the operation picks one root and says which rule it applied.
        _text, result = self._overview("tools", "tests")
        self.assertEqual(result["status"], "ready")
        self.assertIn("overview-root-first-prefix", result["warnings"])
        self.assertEqual(result["overview"]["root"], "tests/")
        counted = next(
            group["file_count"]
            for group in self.fixture["repository"]["groups"]
            if group["path_prefix"] == "tests/"
        )
        # The subtree's own count must agree with the row the whole-repository
        # table reports for it.
        self.assertEqual(result["overview"]["counted_file_count"], counted)


    def test_the_output_budget_is_what_sizes_the_table(self) -> None:
        # The table has no fixed width any more: the engine returns every
        # group and the broker folds the tail into `"*"` until the answer fits
        # the caller's budget, so the same repository answers with a wider
        # table the more room it is given.
        matrix = self.fixture["budgets"]
        reserve = matrix["finding_reserve"]
        for key, prefixes in (("repository", ()), ("subtree", (self.fixture["subtree"]["path_prefix"],))):
            width = len(self.fixture[key]["groups"])
            widths: list[int] = []
            for expected in matrix[key]:
                budget = expected["maximum_output_characters"]
                with self.subTest(request=key, budget=budget):
                    _text, result = self._overview(*prefixes, budget=budget)
                    rows = [
                        group
                        for group in result["groups"]
                        if group["path_prefix"] != FOLDED_PREFIX
                    ]
                    folded = [
                        group
                        for group in result["groups"]
                        if group["path_prefix"] == FOLDED_PREFIX
                    ]
                    self.assertEqual(len(rows), expected["directory_rows"])
                    self.assertEqual(
                        result["overview"]["other_group_count"],
                        expected["other_group_count"],
                    )
                    # Every directory the answer stopped naming is counted in
                    # exactly one folded row, so the reader can still add the
                    # whole subtree up whatever the budget was.
                    self.assertEqual(
                        len(rows) + int(result["overview"]["other_group_count"]), width
                    )
                    self.assertEqual(
                        len(folded), 1 if expected["other_group_count"] else 0
                    )
                    if folded:
                        self.assertIsNone(folded[0]["representative_identity"])
                        self.assertEqual(folded[0]["depth"], 0)
                        self.assertEqual(
                            result["groups"][-1]["path_prefix"], FOLDED_PREFIX
                        )
                    # The file layer is what the fold reserves room for, and
                    # the answer never silently overruns the budget it was
                    # given.
                    self.assertEqual(result["returned_count"], expected["returned_count"])
                    self.assertGreaterEqual(result["returned_count"], reserve)
                    self.assertNotIn("output-budget-exceeded", result["warnings"])
                    self.assertLessEqual(result["output_characters"], budget)
                    widths.append(len(rows))
            # A wider budget never buys a narrower table.
            self.assertEqual(widths, sorted(widths), key)

    def test_a_root_that_is_not_a_directory_is_answered_with_a_warning(self) -> None:
        # README.md is a real path at the pinned commit but not a directory, so
        # the table is empty and the warning is what tells that apart from a
        # directory the query counted nothing in.
        _text, result = self._overview("README.md")
        self.assertEqual(result["status"], "ready")
        self.assertIn("overview-root-not-a-directory", result["warnings"])
        self.assertEqual(result["overview"]["root"], "README.md/")
        self.assertEqual(result["overview"]["counted_file_count"], 0)
        self.assertEqual(result["groups"], [])
        # A real directory answers without it.
        _subtree_text, subtree = self._overview("tools")
        self.assertNotIn("overview-root-not-a-directory", subtree["warnings"])


if __name__ == "__main__":
    unittest.main()
