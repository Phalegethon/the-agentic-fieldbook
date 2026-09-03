"""Opt-in end-to-end query latency benchmark through the broker.

Run from the repository root with a built native engine:

    TAF_DOGFOOD=1 TAF_LEVEL1_BINARY=/path/to/taf-level1 \
        python3 -m tests.taf_context.benchmark_query_latency --repo . --query collect_snapshot

It builds the index in a scratch state root, warms the caches with one
query, then reports the median and minimum of N repetitions for each stage:
Python import, Git snapshot, native status, the native query operation, and
the end-to-end broker query. Timing is evidence for the execution ledger,
never a CI gate.

With `--edit`, the script first clones `--repo` into the scratch directory
(the real checkout is never modified) and builds there. After measuring the
usual stages against the unedited clone, it also appends a small function to
`--edit-file` before each of N further end-to-end queries, so every one of
those queries performs a real incremental refresh; the result is reported as
the `end-to-end-after-edit` stage.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = ROOT / "tools" / "taf-context"
ENTRYPOINT = ROOT / "skills" / "prepare-repo-context" / "scripts" / "prepare_repo_context.py"


def _timed(run: Callable[[], object], repetitions: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        run()
        samples.append(time.perf_counter() - started)
    return {
        "median_seconds": round(statistics.median(samples), 4),
        "min_seconds": round(min(samples), 4),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--operation", default="search-symbols")
    parser.add_argument("--query", default="collect_snapshot")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--edit", action="store_true")
    parser.add_argument("--edit-file", default="tools/taf-context/taf_context/state_paths.py")
    args = parser.parse_args(argv)
    if os.environ.get("TAF_DOGFOOD") != "1" or not os.environ.get("TAF_LEVEL1_BINARY"):
        print("set TAF_DOGFOOD=1 and TAF_LEVEL1_BINARY to run the latency benchmark", file=sys.stderr)
        return 2

    sys.path.insert(0, str(PACKAGE_ROOT))
    from taf_context import prepare_cli  # noqa: E402
    from taf_context.git_snapshot import collect_snapshot  # noqa: E402

    source_repository = Path(args.repo).resolve()
    with tempfile.TemporaryDirectory(prefix="taf-latency-") as directory:
        environment = {
            "HOME": directory,
            "PATH": os.environ.get("PATH", ""),
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TAF_LEVEL1_BINARY": os.environ["TAF_LEVEL1_BINARY"],
            "TAF_STATE_HOME": str(Path(directory) / "state"),
        }

        def broker(*command: str) -> dict[str, object]:
            completed = subprocess.run(
                [sys.executable, str(ENTRYPOINT), *command],
                env=environment, capture_output=True, text=True, check=False,
            )
            if completed.returncode != 0:
                raise SystemExit(f"broker failed: {completed.stderr.strip()}")
            return json.loads(completed.stdout)

        if args.edit:
            repository = Path(directory) / "repo"
            subprocess.run(
                ["git", "clone", "-q", "--no-hardlinks", str(source_repository), str(repository)],
                check=True, capture_output=True,
            )
        else:
            repository = source_repository

        built = broker("build", "--repo", str(repository), "--confirm-state-write")
        if built["next_safe_action"] != "use-index":
            raise SystemExit("build did not become ready")
        query_command = ("query", "--repo", str(repository), "--operation", args.operation)
        if args.operation in {"search-symbols", "search-docs"}:
            query_command += ("--query", args.query)
        first = broker(*query_command)  # warm caches once

        snapshot = collect_snapshot(repository)
        paths = prepare_cli._state_paths(environment)
        state_root, binding_path = prepare_cli._repository_state_paths(
            paths.root, snapshot.repository_identity, snapshot.worktree_identity
        )
        binary, _source = prepare_cli._resolve_native_binary(environment, paths.root)
        binding = prepare_cli._read_binding(binding_path, snapshot)
        if binary is None or binding is None:
            raise SystemExit("bound native context is unavailable")
        query_text = args.query if args.operation in {"search-symbols", "search-docs"} else None

        stages = {
            "python-import": _timed(
                lambda: subprocess.run(
                    [sys.executable, "-c", "import taf_context.cli"],
                    cwd=str(PACKAGE_ROOT), env=environment, check=True,
                ),
                args.repetitions,
            ),
            "collect-snapshot": _timed(lambda: collect_snapshot(repository), args.repetitions),
            "native-status": _timed(
                lambda: prepare_cli._invoke_native(
                    binary, "status", repository, state_root, snapshot, index_identity=binding.index_identity
                ),
                args.repetitions,
            ),
            f"native-{args.operation}": _timed(
                lambda: prepare_cli._invoke_native(
                    binary, args.operation, repository, state_root, snapshot,
                    index_identity=binding.index_identity, query=query_text,
                ),
                args.repetitions,
            ),
            "end-to-end": _timed(lambda: broker(*query_command), args.repetitions),
        }

        edit_changed_path_count: int | None = None
        if args.edit:
            edit_file = repository / args.edit_file
            samples: list[float] = []
            for marker in range(1, args.repetitions + 1):
                with edit_file.open("a", encoding="utf-8") as handle:
                    handle.write(f"\n\ndef benchmark_marker_{marker}():\n    return {marker}\n")
                started = time.perf_counter()
                edit_result = broker(*query_command)
                samples.append(time.perf_counter() - started)
                edit_changed_path_count = edit_result["refresh"]["changed_path_count"]
            stages["end-to-end-after-edit"] = {
                "median_seconds": round(statistics.median(samples), 4),
                "min_seconds": round(min(samples), 4),
            }

        report = {
            "schema_version": "1",
            "repository": repository.name,
            "operation": args.operation,
            "query": query_text,
            "repetitions": args.repetitions,
            "status": first["status"],
            "returned_count": first["returned_count"],
            "stages": stages,
        }
        if args.edit:
            report["edit"] = {"edit_file": args.edit_file, "changed_path_count": edit_changed_path_count}
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
