"""Behavioral tests for the portable Level 1 wire contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from taf_context.level1_models import (
    CandidateAvailability,
    CandidateManifest,
    Level1Finding,
    Level1Filters,
    Level1Operation,
    Level1RecordKind,
    Level1Request,
    Level1Result,
    Level1ResultStatus,
    Level1SourceType,
    parse_level1_request,
    parse_level1_result,
)


REPOSITORY_IDENTITY = "sha256:" + "1" * 64
WORKTREE_IDENTITY = "sha256:" + "2" * 64
DIRTY_IDENTITY = "sha256:" + "3" * 64
INDEX_IDENTITY = "sha256:" + "4" * 64
RESULT_IDENTITY = "sha256:" + "5" * 64
HEAD = "a" * 40


def request_wire(operation: str = "search-symbols") -> dict[str, object]:
    query = "RecoveryDossier" if operation in {"search-symbols", "search-docs"} else None
    result_identities = [RESULT_IDENTITY] if operation == "source-snippets" else []
    index_identity = None if operation in {"estimate", "build"} else INDEX_IDENTITY
    filters = {
        "path_prefixes": ["tools/taf-context"],
        "languages": ["Python"],
        "symbol_kinds": ["class"],
        "source_types": ["source"],
    }
    if operation in {"estimate", "build", "update", "status", "metrics"}:
        filters = {
            "path_prefixes": [],
            "languages": [],
            "symbol_kinds": [],
            "source_types": [],
        }
    return {
        "schema_version": "1",
        "request_identity": "request-0001",
        "consumer_identity": "taf.work-recovery",
        "operation": operation,
        "repository_identity": REPOSITORY_IDENTITY,
        "worktree_identity": WORKTREE_IDENTITY,
        "committed_head": HEAD,
        "dirty_overlay_fingerprint": DIRTY_IDENTITY,
        "provider_identity": "taf.native.level1",
        "index_identity": index_identity,
        "required_capability": operation,
        "minimum_freshness": "exact",
        "query": query,
        "result_identities": result_identities,
        "filters": filters,
        "maximum_results": 10,
        "maximum_model_output_characters": 4000,
        "allow_inferred": False,
    }


def finding_wire() -> dict[str, object]:
    return {
        "rank": 1,
        "result_identity": RESULT_IDENTITY,
        "path": "tools/taf-context/taf_context/recovery.py",
        "start_line": 10,
        "end_line": 14,
        "language": "Python",
        "record_kind": "definition",
        "source_type": "source",
        "qualified_name": "taf_context.recovery.RecoveryDossier",
        "extraction_method": "tree-sitter-python@0.25.0",
        "evidence_class": "verified",
        "preview": "class RecoveryDossier:",
    }


def result_wire() -> dict[str, object]:
    return {
        "schema_version": "1",
        "request_identity": "request-0001",
        "operation": "search-symbols",
        "status": "ready",
        "provider_identity": "taf.native.level1",
        "provider_version": "0.1.0",
        "index_identity": INDEX_IDENTITY,
        "repository_identity": REPOSITORY_IDENTITY,
        "worktree_identity": WORKTREE_IDENTITY,
        "committed_head": HEAD,
        "dirty_overlay_fingerprint": DIRTY_IDENTITY,
        "freshness": "exact",
        "parser_versions": {"tree-sitter-python": "0.25.0"},
        "coverage": {
            "path_coverage": 1.0,
            "language_coverage": 1.0,
            "indexed_path_count": 1,
            "excluded_path_count": 0,
            "unsupported_language_count": 0,
            "parse_failure_count": 0,
            "exclusion_reason_counts": {},
        },
        "findings": [finding_wire()],
        "returned_count": 1,
        "omitted_count": 0,
        "truncated": False,
        "output_characters": 321,
        "warnings": [],
        "next_safe_action": "use-cited-evidence",
    }


def ready_candidate_wire() -> dict[str, object]:
    return {
        "schema_version": "1",
        "candidate_identity": "python-tree-sitter-sqlite",
        "candidate_version": "0.1.0",
        "language": "Python",
        "protocol_version": "1",
        "availability": "ready",
        "unsupported_reason_codes": [],
        "executable": ".venv/bin/python",
        "arguments": ["-m", "taf_level1_candidate"],
        "environment_allowlist": ["LANG", "PATH"],
        "declared_child_processes": [],
        "dependency_lock": "uv.lock",
        "license_inventory": "licenses.json",
    }


class Level1VocabularyTests(unittest.TestCase):
    def test_wire_enums_expose_only_the_frozen_values(self) -> None:
        self.assertEqual(
            [item.value for item in Level1Operation],
            [
                "estimate", "build", "update", "status", "metrics",
                "repository-map", "search-symbols", "search-docs",
                "source-snippets",
            ],
        )
        self.assertEqual(
            [item.value for item in Level1RecordKind],
            [
                "module", "definition", "import", "entry-point",
                "configuration", "heading", "document-chunk",
            ],
        )
        self.assertEqual(
            [item.value for item in Level1SourceType],
            ["source", "document", "configuration"],
        )
        self.assertEqual(
            [item.value for item in Level1ResultStatus],
            ["ready", "partial", "stale", "unsupported", "error"],
        )
        self.assertEqual(
            [item.value for item in CandidateAvailability],
            ["ready", "unsupported"],
        )


class Level1RequestTests(unittest.TestCase):
    def test_read_request_round_trip_is_exact_and_immutable(self) -> None:
        wire = request_wire()
        request = Level1Request.from_dict(wire)

        self.assertEqual(request.to_dict(), wire)
        self.assertIs(request.operation, Level1Operation.SEARCH_SYMBOLS)
        self.assertEqual(
            request.filters,
            Level1Filters(
                ("tools/taf-context",),
                ("Python",),
                ("class",),
                (Level1SourceType.SOURCE,),
            ),
        )
        with self.assertRaises(Exception):
            request.filters.languages[0] = "Rust"  # type: ignore[index]

    def test_every_operation_accepts_its_canonical_shape(self) -> None:
        for operation in [item.value for item in Level1Operation]:
            with self.subTest(operation=operation):
                wire = request_wire(operation)
                self.assertEqual(Level1Request.from_dict(wire).to_dict(), wire)

    def test_operation_specific_query_identity_and_filter_rules_fail_closed(self) -> None:
        cases: list[dict[str, object]] = []
        missing_query = request_wire("search-symbols")
        missing_query["query"] = None
        cases.append(missing_query)
        map_query = request_wire("repository-map")
        map_query["query"] = "not allowed"
        cases.append(map_query)
        snippets_without_ids = request_wire("source-snippets")
        snippets_without_ids["result_identities"] = []
        cases.append(snippets_without_ids)
        snippets_with_query = request_wire("source-snippets")
        snippets_with_query["query"] = "not allowed"
        cases.append(snippets_with_query)
        control_with_filter = request_wire("build")
        control_with_filter["filters"] = request_wire()["filters"]
        cases.append(control_with_filter)
        build_with_index = request_wire("build")
        build_with_index["index_identity"] = INDEX_IDENTITY
        cases.append(build_with_index)
        query_without_index = request_wire("search-docs")
        query_without_index["index_identity"] = None
        cases.append(query_without_index)

        for wire in cases:
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_rejects_invalid_budgets_counts_paths_sets_and_scalar_types(self) -> None:
        cases: list[dict[str, object]] = []
        for budget in (1999, 2001, 4001, 8001, 12001, True):
            wire = request_wire()
            wire["maximum_model_output_characters"] = budget
            cases.append(wire)
        for count in (0, 65, True):
            wire = request_wire()
            wire["maximum_results"] = count
            cases.append(wire)
        for path in ("/absolute", "../escape", "C:/escape", "a\\b"):
            wire = request_wire()
            filters = copy.deepcopy(wire["filters"])
            assert isinstance(filters, dict)
            filters["path_prefixes"] = [path]
            wire["filters"] = filters
            cases.append(wire)
        unsorted = request_wire()
        filters = copy.deepcopy(unsorted["filters"])
        assert isinstance(filters, dict)
        filters["languages"] = ["Rust", "Python"]
        unsorted["filters"] = filters
        cases.append(unsorted)
        duplicate = request_wire()
        duplicate["result_identities"] = [RESULT_IDENTITY, RESULT_IDENTITY]
        duplicate["operation"] = "source-snippets"
        duplicate["query"] = None
        cases.append(duplicate)
        surrogate = request_wire()
        surrogate["query"] = "\ud800"
        cases.append(surrogate)

        for wire in cases:
            with self.subTest(wire=wire):
                with self.assertRaises((TypeError, ValueError)):
                    Level1Request.from_dict(wire)

    def test_rejects_missing_unknown_and_invalid_identity_fields(self) -> None:
        cases: list[dict[str, object]] = []
        missing = request_wire()
        del missing["consumer_identity"]
        cases.append(missing)
        unknown = request_wire()
        unknown["command"] = ["run"]
        cases.append(unknown)
        bad_schema = request_wire()
        bad_schema["schema_version"] = "2"
        cases.append(bad_schema)
        for field, value in (
            ("repository_identity", "sha256:short"),
            ("worktree_identity", "not-a-digest"),
            ("committed_head", "abc"),
            ("dirty_overlay_fingerprint", "sha256:not-hex"),
            ("provider_identity", "../provider"),
            ("request_identity", "UPPERCASE"),
        ):
            wire = request_wire()
            wire[field] = value
            cases.append(wire)

        for wire in cases:
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_bounded_parser_rejects_duplicate_nonfinite_and_oversized_json(self) -> None:
        duplicate = b'{"schema_version":"1","schema_version":"1"}'
        nonfinite = json.dumps(request_wire()).replace("4000", "NaN").encode()
        oversized = json.dumps(request_wire()).encode() + b" " * (256 * 1024)

        for raw in (duplicate, nonfinite, oversized, b"\xff"):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises((UnicodeError, ValueError)):
                    parse_level1_request(raw)


class Level1ResultTests(unittest.TestCase):
    def test_result_round_trip_preserves_nested_types_and_exact_fields(self) -> None:
        wire = result_wire()
        result = Level1Result.from_dict(wire)

        self.assertEqual(result.to_dict(), wire)
        self.assertIs(result.status, Level1ResultStatus.READY)
        self.assertEqual(result.findings, (Level1Finding.from_dict(finding_wire()),))
        self.assertEqual(result.findings[0].evidence_class.value, "verified")

    def test_rejects_count_rank_citation_freshness_and_status_lies(self) -> None:
        cases: list[dict[str, object]] = []
        wrong_count = result_wire()
        wrong_count["returned_count"] = 2
        cases.append(wrong_count)
        wrong_rank = result_wire()
        finding = copy.deepcopy(finding_wire())
        finding["rank"] = 2
        wrong_rank["findings"] = [finding]
        cases.append(wrong_rank)
        absolute = result_wire()
        finding = copy.deepcopy(finding_wire())
        finding["path"] = "/private/source.py"
        absolute["findings"] = [finding]
        cases.append(absolute)
        reversed_range = result_wire()
        finding = copy.deepcopy(finding_wire())
        finding["end_line"] = 9
        reversed_range["findings"] = [finding]
        cases.append(reversed_range)
        stale_verified = result_wire()
        stale_verified["freshness"] = "structurally-stale"
        cases.append(stale_verified)
        stale_with_findings = result_wire()
        stale_with_findings["status"] = "stale"
        cases.append(stale_with_findings)
        bad_truncation = result_wire()
        bad_truncation["omitted_count"] = 1
        bad_truncation["truncated"] = False
        cases.append(bad_truncation)
        bad_build = result_wire()
        bad_build["operation"] = "build"
        bad_build["index_identity"] = None
        cases.append(bad_build)
        oversized_model_output = result_wire()
        oversized_model_output["output_characters"] = 12001
        cases.append(oversized_model_output)

        for wire in cases:
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)

    def test_nonready_build_may_omit_index_but_other_results_may_not(self) -> None:
        build = result_wire()
        build.update(
            {
                "operation": "build",
                "status": "error",
                "index_identity": None,
                "freshness": "unusable",
                "findings": [],
                "returned_count": 0,
                "output_characters": 100,
                "next_safe_action": "fix-candidate",
            }
        )
        self.assertEqual(Level1Result.from_dict(build).to_dict(), build)

        status = copy.deepcopy(build)
        status["operation"] = "status"
        with self.assertRaises(ValueError):
            Level1Result.from_dict(status)

    def test_result_parser_rejects_unknown_duplicate_nonfinite_and_oversized(self) -> None:
        unknown = result_wire()
        unknown["model_text"] = "not in contract"
        duplicate = b'{"schema_version":"1","schema_version":"1"}'
        nonfinite_wire = result_wire()
        coverage = copy.deepcopy(nonfinite_wire["coverage"])
        assert isinstance(coverage, dict)
        coverage["path_coverage"] = float("nan")
        nonfinite_wire["coverage"] = coverage
        nonfinite = json.dumps(nonfinite_wire).encode()
        oversized = json.dumps(result_wire()).encode() + b" " * (256 * 1024)

        with self.assertRaises(ValueError):
            Level1Result.from_dict(unknown)
        for raw in (duplicate, nonfinite, oversized):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ValueError):
                    parse_level1_result(raw)


class CandidateManifestTests(unittest.TestCase):
    def test_ready_and_unsupported_manifests_have_disjoint_requirements(self) -> None:
        ready = ready_candidate_wire()
        self.assertEqual(CandidateManifest.from_dict(ready).to_dict(), ready)

        unsupported = copy.deepcopy(ready)
        unsupported.update(
            {
                "availability": "unsupported",
                "unsupported_reason_codes": ["unsupported-toolchain"],
                "executable": "",
                "arguments": [],
                "environment_allowlist": [],
                "dependency_lock": "",
                "license_inventory": "",
            }
        )
        self.assertEqual(
            CandidateManifest.from_dict(unsupported).to_dict(),
            unsupported,
        )

        ready_with_reason = copy.deepcopy(ready)
        ready_with_reason["unsupported_reason_codes"] = ["unsupported-toolchain"]
        unsupported_with_path = copy.deepcopy(unsupported)
        unsupported_with_path["executable"] = "bin/candidate"
        for wire in (ready_with_reason, unsupported_with_path):
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    CandidateManifest.from_dict(wire)

    def test_manifest_rejects_unsafe_paths_unsorted_sets_and_executable_fields(self) -> None:
        cases: list[dict[str, object]] = []
        for field, value in (
            ("executable", "../candidate"),
            ("dependency_lock", "/tmp/lock"),
            ("license_inventory", "C:/licenses"),
        ):
            wire = ready_candidate_wire()
            wire[field] = value
            cases.append(wire)
        unsorted = ready_candidate_wire()
        unsorted["environment_allowlist"] = ["PATH", "LANG"]
        cases.append(unsorted)
        child = ready_candidate_wire()
        child["declared_child_processes"] = ["curl"]
        cases.append(child)
        unknown = ready_candidate_wire()
        unknown["install"] = ["brew", "install"]
        cases.append(unknown)

        for wire in cases:
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    CandidateManifest.from_dict(wire)


class ContractSchemaTests(unittest.TestCase):
    def test_json_schemas_publish_the_same_top_level_fields_and_enums(self) -> None:
        root = Path(__file__).parents[2] / "tools" / "taf-context" / "contracts" / "level1"
        request_schema = json.loads((root / "request.schema.json").read_text())
        result_schema = json.loads((root / "result.schema.json").read_text())

        self.assertFalse(request_schema["additionalProperties"])
        self.assertFalse(result_schema["additionalProperties"])
        self.assertEqual(
            set(request_schema["properties"]),
            set(Level1Request.__dataclass_fields__),
        )
        self.assertEqual(
            set(result_schema["properties"]),
            set(Level1Result.__dataclass_fields__),
        )
        self.assertEqual(
            request_schema["properties"]["operation"]["enum"],
            [item.value for item in Level1Operation],
        )
        conditioned_operations: set[str] = set()
        for branch in request_schema["allOf"]:
            operation = branch["if"]["properties"]["operation"]
            conditioned_operations.update(
                operation.get("enum", [operation.get("const")])
            )
        self.assertEqual(
            conditioned_operations,
            {item.value for item in Level1Operation},
        )
        self.assertEqual(
            result_schema["properties"]["status"]["enum"],
            [item.value for item in Level1ResultStatus],
        )
        self.assertEqual(
            result_schema["$defs"]["finding"]["properties"]["record_kind"]["enum"],
            [item.value for item in Level1RecordKind],
        )


if __name__ == "__main__":
    unittest.main()
