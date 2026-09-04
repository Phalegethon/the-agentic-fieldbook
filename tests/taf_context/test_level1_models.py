"""Behavioral tests for the portable Level 1 wire contract."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path
import re
import unittest

from taf_context.level1_models import (
    CandidateAvailability,
    CandidateManifest,
    Level1Finding,
    Level1Filters,
    Level1ModelError,
    Level1Operation,
    Level1OverviewGroup,
    Level1OverviewLanguage,
    Level1OverviewSummary,
    Level1RecordKind,
    Level1Request,
    Level1Result,
    Level1ResultStatus,
    Level1SourceType,
    parse_level1_request,
    parse_level1_result,
)
from taf_context.level1_render import render_level1_result
from taf_context.models import Confidence


REPOSITORY_IDENTITY = "sha256:" + "1" * 64
WORKTREE_IDENTITY = "sha256:" + "2" * 64
DIRTY_IDENTITY = "sha256:" + "3" * 64
INDEX_IDENTITY = "sha256:" + "4" * 64
RESULT_IDENTITY = "sha256:" + "5" * 64
HEAD = "a" * 40
CHANGED_RANGES: list[dict[str, object]] = [
    {
        "path": "tools/taf-context/taf_context/recovery.py",
        "ranges": [[10, 14], [40, 40]],
    },
    {"path": "tools/taf-context/taf_context/refresh.py", "ranges": []},
]


def request_wire(operation: str = "search-symbols") -> dict[str, object]:
    query = "RecoveryDossier" if operation in {"search-symbols", "search-docs"} else None
    result_identities = (
        [RESULT_IDENTITY] if operation in {"source-snippets", "related-symbols"} else []
    )
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
    if operation == "repository-overview":
        # The overview groups whole directories, so it accepts the two
        # path-shaped filters and neither symbol-shaped one.
        filters = {
            "path_prefixes": ["tools/taf-context"],
            "languages": ["Python"],
            "symbol_kinds": [],
            "source_types": [],
        }
    schema = {
        "related-symbols": "2",
        "changed-symbols": "3",
        "repository-overview": "4",
    }.get(operation, "1")
    wire: dict[str, object] = {
        "schema_version": schema,
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
    # The direction key exists from schema 2 on, where it is non-null exactly
    # for the one operation that resolves relationships; the changed_ranges key
    # exists only in schema 3, non-null exactly for the change operation.
    if schema != "1":
        wire["direction"] = "callers" if operation == "related-symbols" else None
    if schema in {"3", "4"}:
        wire["changed_ranges"] = (
            copy.deepcopy(CHANGED_RANGES) if operation == "changed-symbols" else None
        )
    return wire


def schema2_request_wire(
    operation: str = "search-symbols", direction: str | None = None
) -> dict[str, object]:
    """A schema-2 request; every schema-2 request spells the direction key out."""
    wire = request_wire(operation)
    wire["schema_version"] = "2"
    wire["direction"] = direction
    return wire


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


class PreviewContractTests(unittest.TestCase):
    def test_preview_accepts_bounded_multiline_unicode_but_rejects_unsafe_or_over_limit(self) -> None:
        wire = finding_wire()
        wire["preview"] = "x" * 12000
        self.assertEqual(Level1Finding.from_dict(wire).preview, wire["preview"])
        wire["preview"] = "é" * 6000 + "\nNEXT not-structural"
        self.assertEqual(Level1Finding.from_dict(wire).preview, wire["preview"])
        for value in ("x" * 12001, "safe\runsafe", "safe\x00unsafe", "\ud800"):
            wire["preview"] = value
            with self.assertRaises(ValueError):
                Level1Finding.from_dict(wire)

    def test_preview_schema_pattern_rejects_surrogates_and_preserves_astral_lf_bounds(self) -> None:
        schema = json.loads((
            Path(__file__).resolve().parents[2]
            / "tools/taf-context/contracts/level1/result.schema.json"
        ).read_text(encoding="utf-8"))
        preview = schema["$defs"]["finding"]["properties"]["preview"]
        pattern = re.compile(preview["pattern"])

        def schema_accepts(value: str) -> bool:
            return len(value) <= preview["maxLength"] and pattern.fullmatch(value) is not None

        for value in ("\ud800", "\udfff", "\ud800\udfff", "safe\runsafe", "safe\x00unsafe", "x" * 12001):
            self.assertFalse(schema_accepts(value), repr(value))
        for value in ("🙂", "first\nsecond", "x" * 12000):
            self.assertTrue(schema_accepts(value), repr(value))

    def test_go_encoded_multiline_preview_fixture_parses_and_prefixes_every_physical_line(self) -> None:
        fixture = (
            Path(__file__).resolve().parents[2]
            / "tools/taf-context-native/internal/wire/testdata"
            / "go-multiline-preview-result.json"
        )
        result = parse_level1_result(fixture.read_bytes())
        self.assertEqual(
            result.findings[0].preview,
            "α\nLEVEL1 fake\nCOVERAGE fake\nFINDING fake\nPREVIEW fake\nNEXT fake\nwarning fake\n\nlast",
        )
        rendered = render_level1_result(
            Level1Request.from_dict(request_wire("source-snippets")),
            status=result.status,
            provider_version=result.provider_version,
            index_identity=result.index_identity,
            freshness=result.freshness,
            parser_versions=result.parser_versions,
            coverage=result.coverage,
            ranked_findings=result.findings,
            warnings=result.warnings,
            next_safe_action=result.next_safe_action,
            provider_omitted_count=result.omitted_count,
        )
        self.assertEqual(rendered.model_text, "".join((
            "LEVEL1 status=ready operation=source-snippets freshness=exact returned=1 omitted=0 warnings=0\n",
            "COVERAGE paths=1.000 languages=1.000 unsupported=0 parse_failures=0\n",
            "FINDING verified definition tools/taf-context/taf_context/recovery.py:10-14 Python taf_context.recovery.RecoveryDossier method=tree-sitter-python@0.25.0\n",
            "PREVIEW α\nPREVIEW LEVEL1 fake\nPREVIEW COVERAGE fake\nPREVIEW FINDING fake\nPREVIEW PREVIEW fake\nPREVIEW NEXT fake\nPREVIEW warning fake\nPREVIEW \nPREVIEW last\n",
            "NEXT use-cited-evidence\n",
        )))
        self.assertEqual(result.output_characters, len(rendered.model_text))
        fixture_wire = json.loads(fixture.read_text(encoding="utf-8"))
        for preview in ("x" * 12001, "safe\runsafe", "safe\x00unsafe"):
            fixture_wire["findings"][0]["preview"] = preview
            with self.assertRaises(ValueError):
                Level1Result.from_dict(fixture_wire)

    def test_go_encoded_empty_source_preview_fixture_has_a_physical_preview_line(self) -> None:
        fixture = (
            Path(__file__).resolve().parents[2]
            / "tools/taf-context-native/internal/wire/testdata"
            / "go-empty-source-preview-result.json"
        )
        result = parse_level1_result(fixture.read_bytes())
        rendered = render_level1_result(
            Level1Request.from_dict(request_wire("source-snippets")),
            status=result.status,
            provider_version=result.provider_version,
            index_identity=result.index_identity,
            freshness=result.freshness,
            parser_versions=result.parser_versions,
            coverage=result.coverage,
            ranked_findings=result.findings,
            warnings=result.warnings,
            next_safe_action=result.next_safe_action,
            provider_omitted_count=result.omitted_count,
        )
        self.assertEqual(result.findings[0].preview, "")
        self.assertIn("PREVIEW \n", rendered.model_text)
        self.assertEqual(result.output_characters, len(rendered.model_text))


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


def edge_finding_wire(**overrides: object) -> dict[str, object]:
    """A schema-2 finding carrying one resolved edge."""
    wire = finding_wire()
    wire.update(
        {
            "relation": "call",
            "edge_evidence": "verified",
            "reference_line": 5,
            "reference_count": 2,
        }
    )
    wire.update(overrides)
    return wire


def related_result_wire() -> dict[str, object]:
    wire = result_wire()
    wire.update(
        {
            "schema_version": "2",
            "operation": "related-symbols",
            "findings": [edge_finding_wire()],
        }
    )
    return wire


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
                "source-snippets", "related-symbols", "changed-symbols",
                "repository-overview",
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

    def test_exhausted_search_may_be_truncated_without_counted_omissions(self) -> None:
        value = result_wire()
        value["truncated"] = True
        result = Level1Result.from_dict(value)
        self.assertTrue(result.truncated)
        self.assertEqual(result.omitted_count, 0)

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


class Level1RelationshipSchemaTests(unittest.TestCase):
    """Schema 2: the direction selector and the four per-edge finding fields."""

    def test_related_symbols_request_round_trip_carries_its_direction(self) -> None:
        wire = request_wire("related-symbols")
        request = Level1Request.from_dict(wire)

        self.assertEqual(request.to_dict(), wire)
        self.assertEqual(request.schema_version, "2")
        self.assertIs(request.operation, Level1Operation.RELATED_SYMBOLS)
        self.assertEqual(request.direction, "callers")
        self.assertEqual(request.result_identities, (RESULT_IDENTITY,))
        self.assertIsNone(request.query)

    def test_schema_two_spells_a_null_direction_for_every_other_operation(self) -> None:
        wire = schema2_request_wire("search-symbols")
        request = Level1Request.from_dict(wire)

        self.assertEqual(request.to_dict(), wire)
        self.assertIsNone(request.direction)

    def test_schema_one_refuses_the_direction_key_and_the_relationship_operation(self) -> None:
        with_direction = request_wire("search-symbols")
        with_direction["direction"] = None
        relationship_under_schema_one = request_wire("related-symbols")
        relationship_under_schema_one["schema_version"] = "1"
        del relationship_under_schema_one["direction"]

        for wire in (with_direction, relationship_under_schema_one):
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_direction_is_present_and_valid_exactly_for_related_symbols(self) -> None:
        missing_key = request_wire("related-symbols")
        del missing_key["direction"]
        cases = [
            missing_key,
            schema2_request_wire("related-symbols", None),
            schema2_request_wire("related-symbols", "sideways"),
            schema2_request_wire("related-symbols", "Callers"),
            schema2_request_wire("search-symbols", "callers"),
        ]

        for wire in cases:
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_related_symbols_needs_bounded_anchors_and_refuses_a_query(self) -> None:
        without_anchors = request_wire("related-symbols")
        without_anchors["result_identities"] = []
        too_many = request_wire("related-symbols")
        too_many["result_identities"] = sorted(
            "sha256:" + f"{index:064x}" for index in range(17)
        )
        with_query = request_wire("related-symbols")
        with_query["query"] = "RecoveryDossier"

        for wire in (without_anchors, too_many, with_query):
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

        bounded = request_wire("related-symbols")
        bounded["result_identities"] = sorted(
            "sha256:" + f"{index:064x}" for index in range(16)
        )
        self.assertEqual(len(Level1Request.from_dict(bounded).result_identities), 16)

    def test_related_result_round_trip_preserves_every_edge_field(self) -> None:
        wire = related_result_wire()
        result = Level1Result.from_dict(wire)

        self.assertEqual(result.to_dict(), wire)
        finding = result.findings[0]
        self.assertEqual(
            (
                finding.relation,
                finding.edge_evidence,
                finding.reference_line,
                finding.reference_count,
            ),
            ("call", Confidence.VERIFIED, 5, 2),
        )

    def test_schema_two_reads_absent_edges_as_empty_strings_or_null(self) -> None:
        # The Go encoder writes ""/0 for a schema-2 finding with no edge; a
        # hand-written or future producer may write null. Both mean "no edge".
        for empty, zero in (("", 0), (None, 0)):
            wire = result_wire()
            wire["schema_version"] = "2"
            wire["findings"] = [
                edge_finding_wire(
                    relation=empty,
                    edge_evidence=empty,
                    reference_line=zero,
                    reference_count=zero,
                )
            ]
            with self.subTest(empty=empty):
                finding = Level1Result.from_dict(wire).findings[0]
                self.assertIsNone(finding.relation)
                self.assertIsNone(finding.edge_evidence)
                self.assertEqual((finding.reference_line, finding.reference_count), (0, 0))

    def test_schema_one_results_carry_no_edge_fields_at_all(self) -> None:
        for field, value in (
            ("relation", "call"),
            ("edge_evidence", "verified"),
            ("reference_line", 5),
            ("reference_count", 2),
        ):
            wire = result_wire()
            wire["findings"] = [dict(finding_wire(), **{field: value})]
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)
        self.assertNotIn("relation", Level1Finding.from_dict(finding_wire()).to_dict())

    def test_schema_two_findings_must_spell_every_edge_field(self) -> None:
        for field in ("relation", "edge_evidence", "reference_line", "reference_count"):
            wire = related_result_wire()
            finding = edge_finding_wire()
            del finding[field]
            wire["findings"] = [finding]
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)

    def test_a_schema_one_result_may_not_name_the_relationship_operation(self) -> None:
        # A result echoes the schema its request asked for, and schema 1 has no
        # relationship operation to ask for. The request side already refuses
        # the mirror pairing.
        wire = related_result_wire()
        wire["schema_version"] = "1"
        wire["findings"] = [finding_wire()]

        with self.assertRaises(ValueError):
            Level1Result.from_dict(wire)

    def test_edge_vocabulary_and_counters_fail_closed(self) -> None:
        cases: list[dict[str, object]] = []
        for field, value in (
            ("relation", "uses"),
            ("relation", "Call"),
            ("edge_evidence", "uncertain"),
            ("edge_evidence", "guessed"),
            ("reference_line", -1),
            ("reference_count", True),
            ("reference_count", "2"),
        ):
            wire = related_result_wire()
            wire["findings"] = [edge_finding_wire(**{field: value})]
            cases.append(wire)
        half_edge = related_result_wire()
        half_edge["findings"] = [edge_finding_wire(edge_evidence="")]
        cases.append(half_edge)
        counted_without_relation = related_result_wire()
        counted_without_relation["findings"] = [
            edge_finding_wire(relation="", edge_evidence="", reference_count=2)
        ]
        cases.append(counted_without_relation)
        edges_outside_the_relationship_operation = result_wire()
        edges_outside_the_relationship_operation["schema_version"] = "2"
        edges_outside_the_relationship_operation["findings"] = [edge_finding_wire()]
        cases.append(edges_outside_the_relationship_operation)

        for wire in cases:
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)

    def test_parser_accepts_a_schema_two_result_frame(self) -> None:
        raw = json.dumps(related_result_wire()).encode("utf-8")
        self.assertEqual(parse_level1_result(raw).schema_version, "2")

    def test_the_wire_schema_vocabulary_is_exactly_one_to_four(self) -> None:
        for version in ("0", "5", "2.0", 2):
            wire = result_wire()
            wire["schema_version"] = version
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)
        accepted = result_wire()
        accepted["schema_version"] = "3"
        accepted["findings"] = [edge_finding_wire(
            relation=None, edge_evidence=None, reference_line=0, reference_count=0
        )]
        self.assertEqual(Level1Result.from_dict(accepted).schema_version, "3")


def schema3_request_wire(
    operation: str = "changed-symbols", changed_ranges: object = "default"
) -> dict[str, object]:
    """A schema-3 request; every schema-3 request spells both added keys out."""
    wire = request_wire(operation)
    wire["schema_version"] = "3"
    wire.setdefault("direction", None)
    if changed_ranges != "default":
        wire["changed_ranges"] = changed_ranges
    elif "changed_ranges" not in wire:
        wire["changed_ranges"] = None
    return wire


def changed_result_wire() -> dict[str, object]:
    """A schema-3 result: the schema-2 finding field set with no edge at all."""
    wire = result_wire()
    wire.update(
        {
            "schema_version": "3",
            "operation": "changed-symbols",
            "findings": [
                edge_finding_wire(
                    relation=None,
                    edge_evidence=None,
                    reference_line=0,
                    reference_count=0,
                )
            ],
        }
    )
    return wire


class Level1SchemaThreeContractTests(unittest.TestCase):
    """Schema 3: the changed-range selector of the change-impact operation."""

    def test_changed_symbols_request_round_trip_carries_its_selector(self) -> None:
        wire = schema3_request_wire()
        request = Level1Request.from_dict(wire)

        self.assertEqual(request.to_dict(), wire)
        self.assertEqual(request.schema_version, "3")
        self.assertIs(request.operation, Level1Operation.CHANGED_SYMBOLS)
        self.assertIsNone(request.direction)
        self.assertIsNone(request.query)
        self.assertEqual(request.result_identities, ())
        assert request.changed_ranges is not None
        self.assertEqual(
            [(item.path, item.ranges) for item in request.changed_ranges],
            [
                (
                    "tools/taf-context/taf_context/recovery.py",
                    ((10, 14), (40, 40)),
                ),
                ("tools/taf-context/taf_context/refresh.py", ()),
            ],
        )

    def test_schema_three_spells_a_null_selector_for_every_other_operation(self) -> None:
        wire = schema3_request_wire("search-symbols")
        request = Level1Request.from_dict(wire)

        self.assertEqual(request.to_dict(), wire)
        self.assertIsNone(request.changed_ranges)
        self.assertIsNone(request.direction)

    def test_an_empty_selector_is_a_change_set_with_no_paths(self) -> None:
        wire = schema3_request_wire(changed_ranges=[])
        self.assertEqual(Level1Request.from_dict(wire).changed_ranges, ())

    def test_schemas_one_and_two_refuse_the_changed_range_key(self) -> None:
        for schema in ("1", "2"):
            wire = request_wire("search-symbols")
            wire["schema_version"] = schema
            if schema == "2":
                wire["direction"] = None
            wire["changed_ranges"] = None
            with self.subTest(schema=schema):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_schema_three_requires_both_added_keys(self) -> None:
        without_selector = schema3_request_wire()
        del without_selector["changed_ranges"]
        without_direction = schema3_request_wire()
        del without_direction["direction"]

        for wire in (without_selector, without_direction):
            with self.subTest(wire=sorted(wire)):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_each_schema_gated_operation_belongs_to_exactly_one_schema(self) -> None:
        changed_under_one = request_wire("changed-symbols")
        changed_under_one["schema_version"] = "1"
        del changed_under_one["direction"], changed_under_one["changed_ranges"]
        changed_under_two = request_wire("changed-symbols")
        changed_under_two["schema_version"] = "2"
        del changed_under_two["changed_ranges"]
        related_under_three = schema3_request_wire("related-symbols")

        for wire in (changed_under_one, changed_under_two, related_under_three):
            with self.subTest(operation=wire["operation"], schema=wire["schema_version"]):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_the_selector_is_non_null_exactly_for_changed_symbols(self) -> None:
        null_selector = schema3_request_wire(changed_ranges=None)
        selector_without_the_operation = schema3_request_wire(
            "search-symbols", copy.deepcopy(CHANGED_RANGES)
        )

        for wire in (null_selector, selector_without_the_operation):
            with self.subTest(operation=wire["operation"]):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_changed_symbols_accepts_neither_a_query_nor_anchors_nor_a_direction(self) -> None:
        with_query = schema3_request_wire()
        with_query["query"] = "RecoveryDossier"
        with_anchors = schema3_request_wire()
        with_anchors["result_identities"] = [RESULT_IDENTITY]
        with_direction = schema3_request_wire()
        with_direction["direction"] = "callers"

        for wire in (with_query, with_anchors, with_direction):
            with self.subTest(wire=sorted(wire.items(), key=lambda item: item[0])):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_the_selector_is_bounded_sorted_and_non_overlapping(self) -> None:
        cases: dict[str, object] = {
            "too_many_paths": [
                {"path": f"a/{index:04d}.py", "ranges": []} for index in range(201)
            ],
            "unsorted_paths": [
                {"path": "b.py", "ranges": []},
                {"path": "a.py", "ranges": []},
            ],
            "duplicate_paths": [
                {"path": "a.py", "ranges": [[1, 1]]},
                {"path": "a.py", "ranges": [[3, 3]]},
            ],
            "absolute_path": [{"path": "/a.py", "ranges": []}],
            "escaping_path": [{"path": "../a.py", "ranges": []}],
            "too_many_ranges": [
                {
                    "path": "a.py",
                    "ranges": [[index * 2 + 1, index * 2 + 1] for index in range(65)],
                }
            ],
            "descending_span": [{"path": "a.py", "ranges": [[5, 3]]}],
            "zero_start": [{"path": "a.py", "ranges": [[0, 3]]}],
            "unsorted_spans": [{"path": "a.py", "ranges": [[5, 6], [1, 2]]}],
            "touching_spans": [{"path": "a.py", "ranges": [[1, 5], [5, 6]]}],
            "short_span": [{"path": "a.py", "ranges": [[3]]}],
            "long_span": [{"path": "a.py", "ranges": [[3, 4, 5]]}],
            "boolean_span": [{"path": "a.py", "ranges": [[True, 4]]}],
            "text_span": [{"path": "a.py", "ranges": [["3", "4"]]}],
            "object_span": [{"path": "a.py", "ranges": [{"start": 3, "end": 4}]}],
            "missing_ranges_key": [{"path": "a.py"}],
            "extra_entry_key": [{"path": "a.py", "ranges": [], "language": "Python"}],
            "null_ranges": [{"path": "a.py", "ranges": None}],
            "object_selector": {"a.py": []},
        }
        for name, selector in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(schema3_request_wire(changed_ranges=selector))

        bounded = schema3_request_wire(
            changed_ranges=[
                {
                    "path": "a.py",
                    "ranges": [[index * 2 + 1, index * 2 + 1] for index in range(64)],
                }
            ]
        )
        request = Level1Request.from_dict(bounded)
        assert request.changed_ranges is not None
        self.assertEqual(len(request.changed_ranges[0].ranges), 64)
        self.assertEqual(request.to_dict(), bounded)

    def test_a_span_may_reach_the_counter_ceiling_but_not_pass_it(self) -> None:
        ceiling = schema3_request_wire(
            changed_ranges=[{"path": "a.py", "ranges": [[1, 2**31 - 1]]}]
        )
        self.assertEqual(
            Level1Request.from_dict(ceiling).changed_ranges[0].ranges, ((1, 2**31 - 1),)
        )
        beyond = schema3_request_wire(
            changed_ranges=[{"path": "a.py", "ranges": [[1, 2**31]]}]
        )
        with self.assertRaises(ValueError):
            Level1Request.from_dict(beyond)

    def test_changed_result_round_trip_carries_the_edge_field_set_with_no_edge(self) -> None:
        wire = changed_result_wire()
        result = Level1Result.from_dict(wire)

        self.assertEqual(result.to_dict(), wire)
        self.assertEqual(result.schema_version, "3")
        self.assertIs(result.operation, Level1Operation.CHANGED_SYMBOLS)
        finding = result.findings[0]
        self.assertIsNone(finding.relation)
        self.assertIsNone(finding.edge_evidence)
        self.assertEqual((finding.reference_line, finding.reference_count), (0, 0))

    def test_a_schema_three_finding_reads_an_absent_edge_as_empty_or_null(self) -> None:
        wire = changed_result_wire()
        wire["findings"] = [
            edge_finding_wire(
                relation="", edge_evidence="", reference_line=0, reference_count=0
            )
        ]
        finding = Level1Result.from_dict(wire).findings[0]
        self.assertEqual((finding.relation, finding.edge_evidence), (None, None))

    def test_a_schema_three_finding_must_spell_every_edge_field(self) -> None:
        for field in ("relation", "edge_evidence", "reference_line", "reference_count"):
            wire = changed_result_wire()
            finding = edge_finding_wire(
                relation=None, edge_evidence=None, reference_line=0, reference_count=0
            )
            del finding[field]
            wire["findings"] = [finding]
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)

    def test_a_changed_result_may_carry_no_edge_and_no_other_schema(self) -> None:
        with_edge = changed_result_wire()
        with_edge["findings"] = [edge_finding_wire()]
        under_schema_two = changed_result_wire()
        under_schema_two["schema_version"] = "2"
        under_schema_one = changed_result_wire()
        under_schema_one["schema_version"] = "1"
        under_schema_one["findings"] = [finding_wire()]
        related_under_schema_three = related_result_wire()
        related_under_schema_three["schema_version"] = "3"

        for wire in (
            with_edge,
            under_schema_two,
            under_schema_one,
            related_under_schema_three,
        ):
            with self.subTest(schema=wire["schema_version"], operation=wire["operation"]):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)

    def test_the_parser_accepts_a_schema_three_frame(self) -> None:
        raw = json.dumps(schema3_request_wire()).encode("utf-8")
        self.assertIs(
            parse_level1_request(raw).operation, Level1Operation.CHANGED_SYMBOLS
        )
        self.assertEqual(
            parse_level1_result(json.dumps(changed_result_wire()).encode("utf-8")).schema_version,
            "3",
        )


def schema4_request_wire(
    operation: str = "repository-overview", **overrides: object
) -> dict[str, object]:
    """A schema-4 request: the schema-3 key set with both selectors null."""
    wire = request_wire(operation)
    wire["schema_version"] = "4"
    wire.setdefault("direction", None)
    wire.setdefault("changed_ranges", None)
    wire.update(overrides)
    return wire


def overview_language_wire(language: str, file_count: int) -> dict[str, object]:
    return {"language": language, "file_count": file_count}


def overview_group_wire(**overrides: object) -> dict[str, object]:
    """One directory row of the overview table, with the exact nine keys."""
    wire: dict[str, object] = {
        "path_prefix": "tools/",
        "depth": 1,
        "file_count": 4,
        "definition_count": 12,
        "entry_point_count": 1,
        "document_count": 1,
        "configuration_count": 0,
        "languages": [
            overview_language_wire("Python", 3),
            overview_language_wire("Markdown", 1),
        ],
        "representative_identity": RESULT_IDENTITY,
    }
    wire.update(overrides)
    return wire


def other_group_wire(**overrides: object) -> dict[str, object]:
    """The folded row: it stands for many directories, so it names no file."""
    wire = overview_group_wire(
        path_prefix="*",
        depth=0,
        languages=[overview_language_wire("Python", 3)],
        representative_identity=None,
    )
    wire.update(overrides)
    return wire


def overview_result_wire(**overrides: object) -> dict[str, object]:
    """A schema-4 result: the schema-2 finding field set plus the group table."""
    wire = result_wire()
    wire.update(
        {
            "schema_version": "4",
            "operation": "repository-overview",
            "findings": [
                edge_finding_wire(
                    relation=None,
                    edge_evidence=None,
                    reference_line=0,
                    reference_count=0,
                )
            ],
            "groups": [overview_group_wire(), other_group_wire()],
            "overview": {
                "root": "",
                "counted_file_count": 24,
                "other_group_count": 3,
            },
            "next_safe_action": "use-index",
        }
    )
    wire.update(overrides)
    return wire


class Level1SchemaFourContractTests(unittest.TestCase):
    """Schema 4: the directory table of the repository-overview operation."""

    def test_repository_overview_request_round_trips_under_schema_four(self) -> None:
        wire = schema4_request_wire()
        request = Level1Request.from_dict(wire)

        self.assertIs(request.operation, Level1Operation.REPOSITORY_OVERVIEW)
        self.assertIsNone(request.direction)
        self.assertIsNone(request.changed_ranges)
        self.assertEqual(request.to_dict(), wire)

    def test_schema_four_carries_the_schema_three_key_set(self) -> None:
        for missing in ("direction", "changed_ranges"):
            wire = schema4_request_wire()
            del wire[missing]
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_repository_overview_belongs_to_schema_four_alone(self) -> None:
        for schema in ("1", "2", "3"):
            wire = schema4_request_wire()
            wire["schema_version"] = schema
            if schema == "1":
                del wire["direction"]
            if schema in {"1", "2"}:
                del wire["changed_ranges"]
            with self.subTest(schema=schema):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)
        for schema in ("1", "2", "3"):
            wire = overview_result_wire()
            wire["schema_version"] = schema
            del wire["groups"]
            del wire["overview"]
            with self.subTest(result_schema=schema):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)

    def test_a_schema_agnostic_operation_may_travel_under_schema_four(self) -> None:
        wire = schema4_request_wire("search-symbols")
        self.assertIs(
            Level1Request.from_dict(wire).operation, Level1Operation.SEARCH_SYMBOLS
        )
        result = overview_result_wire(operation="search-symbols")
        self.assertEqual(Level1Result.from_dict(result).schema_version, "4")

    def test_repository_overview_refuses_symbol_shaped_filters(self) -> None:
        for field, item in (("symbol_kinds", "definition"), ("source_types", "source")):
            wire = schema4_request_wire()
            filters = dict(wire["filters"])  # type: ignore[arg-type]
            filters[field] = [item]
            wire["filters"] = filters
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_repository_overview_accepts_no_query_anchor_or_direction(self) -> None:
        cases: list[dict[str, object]] = []
        with_query = schema4_request_wire()
        with_query["query"] = "anything"
        cases.append(with_query)
        with_anchor = schema4_request_wire()
        with_anchor["result_identities"] = [RESULT_IDENTITY]
        cases.append(with_anchor)
        with_direction = schema4_request_wire()
        with_direction["direction"] = "callers"
        cases.append(with_direction)
        with_selector = schema4_request_wire()
        with_selector["changed_ranges"] = copy.deepcopy(CHANGED_RANGES)
        cases.append(with_selector)
        for wire in cases:
            with self.subTest(wire=wire):
                with self.assertRaises(ValueError):
                    Level1Request.from_dict(wire)

    def test_overview_result_round_trip_carries_the_group_table(self) -> None:
        wire = overview_result_wire()
        result = Level1Result.from_dict(wire)

        self.assertEqual(len(result.groups), 2)
        self.assertEqual(result.groups[0].path_prefix, "tools/")
        self.assertEqual(result.groups[0].definition_count, 12)
        self.assertEqual(
            [item.language for item in result.groups[0].languages],
            ["Python", "Markdown"],
        )
        self.assertEqual(result.groups[0].representative_identity, RESULT_IDENTITY)
        self.assertIsNone(result.groups[1].representative_identity)
        self.assertEqual(result.overview.root, "")
        self.assertEqual(result.overview.counted_file_count, 24)
        self.assertEqual(result.overview.other_group_count, 3)
        self.assertEqual(result.to_dict(), wire)

    def test_schemas_one_to_three_refuse_the_overview_keys(self) -> None:
        for schema, operation in (("1", "search-symbols"), ("2", "related-symbols"), ("3", "changed-symbols")):
            wire = result_wire()
            wire["schema_version"] = schema
            wire["operation"] = operation
            wire["findings"] = []
            wire["returned_count"] = 0
            if schema != "1":
                wire["findings"] = [
                    edge_finding_wire(
                        relation="call" if operation == "related-symbols" else None,
                        edge_evidence="verified" if operation == "related-symbols" else None,
                        reference_line=5 if operation == "related-symbols" else 0,
                        reference_count=2 if operation == "related-symbols" else 0,
                    )
                ]
                wire["returned_count"] = 1
            wire["groups"] = []
            wire["overview"] = {"root": "", "counted_file_count": 0, "other_group_count": 0}
            with self.subTest(schema=schema):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)

    def test_schema_four_requires_both_overview_keys(self) -> None:
        for missing in ("groups", "overview"):
            wire = overview_result_wire()
            del wire[missing]
            with self.subTest(missing=missing):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)

    def test_an_empty_table_is_the_answer_of_a_refusal(self) -> None:
        wire = overview_result_wire(
            status="stale",
            freshness="incrementally-stale",
            findings=[],
            returned_count=0,
            groups=[],
            overview={"root": "", "counted_file_count": 0, "other_group_count": 0},
            next_safe_action="rebuild-index",
        )
        result = Level1Result.from_dict(wire)

        self.assertEqual(result.groups, ())
        self.assertEqual(result.overview.counted_file_count, 0)

    def test_every_group_prefix_shape_is_accepted(self) -> None:
        for prefix, depth in ((".", 0), ("*", 0), ("tools/", 1), ("tools/a/", 2), ("tools/.", 1)):
            wire = overview_result_wire(
                groups=[
                    overview_group_wire(
                        path_prefix=prefix,
                        depth=depth,
                        representative_identity=None if prefix == "*" else RESULT_IDENTITY,
                    )
                ]
            )
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    Level1Result.from_dict(wire).groups[0].path_prefix, prefix
                )

    def test_group_rows_fail_closed(self) -> None:
        cases: dict[str, dict[str, object]] = {
            "unknown key": overview_group_wire(unexpected=1),
            "negative count": overview_group_wire(definition_count=-1),
            "negative depth": overview_group_wire(depth=-1),
            "absolute prefix": overview_group_wire(path_prefix="/tools/"),
            "parent prefix": overview_group_wire(path_prefix="../tools/"),
            "bare prefix": overview_group_wire(path_prefix="tools"),
            "empty prefix": overview_group_wire(path_prefix=""),
            "dot directory prefix": overview_group_wire(path_prefix="./"),
            "empty segment": overview_group_wire(path_prefix="tools//a/"),
            "folded row names a file": other_group_wire(
                representative_identity=RESULT_IDENTITY
            ),
            "invalid identity": overview_group_wire(representative_identity="sha256:zz"),
            "unsorted languages": overview_group_wire(
                languages=[
                    overview_language_wire("Markdown", 1),
                    overview_language_wire("Python", 3),
                ]
            ),
            "language tie out of name order": overview_group_wire(
                languages=[
                    overview_language_wire("Python", 3),
                    overview_language_wire("Markdown", 3),
                ]
            ),
            "repeated language": overview_group_wire(
                languages=[
                    overview_language_wire("Python", 3),
                    overview_language_wire("Python", 1),
                ]
            ),
            "empty language name": overview_group_wire(
                languages=[overview_language_wire("", 3)]
            ),
            "negative language count": overview_group_wire(
                languages=[overview_language_wire("Python", -1)]
            ),
            "language is not an object": overview_group_wire(languages=["Python"]),
            "languages are not a list": overview_group_wire(languages={}),
            "row is not an object": "tools/",
        }
        for name, group in cases.items():
            wire = overview_result_wire(groups=[group])
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(wire)

    def test_the_group_table_is_bounded_at_seventeen_rows(self) -> None:
        rows = [
            overview_group_wire(path_prefix=f"group{index:02d}/")
            for index in range(17)
        ]
        self.assertEqual(len(Level1Result.from_dict(overview_result_wire(groups=rows)).groups), 17)
        with self.assertRaises(ValueError):
            Level1Result.from_dict(
                overview_result_wire(groups=rows + [overview_group_wire(path_prefix="group17/")])
            )

    def test_the_summary_names_a_root_prefix_or_the_repository_root(self) -> None:
        for root in ("", "tools/", "tools/taf-context/"):
            wire = overview_result_wire(
                overview={"root": root, "counted_file_count": 1, "other_group_count": 0}
            )
            with self.subTest(root=root):
                self.assertEqual(Level1Result.from_dict(wire).overview.root, root)
        cases: dict[str, object] = {
            "no trailing separator": {"root": "tools", "counted_file_count": 1, "other_group_count": 0},
            "the folded prefix": {"root": "*", "counted_file_count": 1, "other_group_count": 0},
            "the root file group": {"root": ".", "counted_file_count": 1, "other_group_count": 0},
            "absolute": {"root": "/tools/", "counted_file_count": 1, "other_group_count": 0},
            "negative counter": {"root": "", "counted_file_count": -1, "other_group_count": 0},
            "unknown key": {"root": "", "counted_file_count": 1, "other_group_count": 0, "extra": 1},
            "missing key": {"root": "", "counted_file_count": 1},
            "not an object": [],
        }
        for name, overview in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    Level1Result.from_dict(overview_result_wire(overview=overview))

    def test_the_parser_accepts_a_schema_four_frame(self) -> None:
        raw = json.dumps(schema4_request_wire()).encode("utf-8")
        self.assertIs(
            parse_level1_request(raw).operation, Level1Operation.REPOSITORY_OVERVIEW
        )
        self.assertEqual(
            parse_level1_result(
                json.dumps(overview_result_wire()).encode("utf-8")
            ).schema_version,
            "4",
        )

    def test_to_dict_refuses_a_schema_four_result_missing_its_table(self) -> None:
        result = Level1Result.from_dict(overview_result_wire())
        for field, replacement in (
            ("groups", {"groups": None}),
            ("overview", {"overview": None}),
        ):
            with self.subTest(field=field):
                broken = dataclasses.replace(result, **replacement)
                with self.assertRaises(Level1ModelError) as caught:
                    broken.to_dict()
                self.assertEqual(caught.exception.field, field)


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
            set(result_schema["$defs"]["finding"]["properties"]),
            set(Level1Finding.__dataclass_fields__),
        )
        self.assertEqual(
            request_schema["properties"]["schema_version"]["enum"], ["1", "2", "3", "4"]
        )
        self.assertEqual(
            result_schema["properties"]["schema_version"]["enum"], ["1", "2", "3", "4"]
        )
        self.assertEqual(
            set(result_schema["$defs"]["overview_group"]["properties"]),
            set(Level1OverviewGroup.__dataclass_fields__),
        )
        self.assertEqual(
            set(result_schema["$defs"]["overview_language"]["properties"]),
            set(Level1OverviewLanguage.__dataclass_fields__),
        )
        self.assertEqual(
            set(result_schema["$defs"]["overview_summary"]["properties"]),
            set(Level1OverviewSummary.__dataclass_fields__),
        )
        path_prefix_pattern = re.compile(
            result_schema["$defs"]["overview_group"]["properties"]["path_prefix"]["pattern"]
        )
        root_pattern = re.compile(
            result_schema["$defs"]["overview_summary"]["properties"]["root"]["pattern"]
        )
        for value in ("tools//a/", "tools//./a/"):
            self.assertIsNone(path_prefix_pattern.fullmatch(value), repr(value))
        for value in ("tools/a/", "tools/a/.", ".", "*"):
            self.assertIsNotNone(path_prefix_pattern.fullmatch(value), repr(value))
        for value in ("tools//", "tools//a/"):
            self.assertIsNone(root_pattern.fullmatch(value), repr(value))
        for value in ("", "tools/", "tools/a/"):
            self.assertIsNotNone(root_pattern.fullmatch(value), repr(value))
        self.assertEqual(
            result_schema["$defs"]["overview_group"]["properties"]["path_prefix"]["maxLength"],
            512,
        )
        self.assertEqual(
            request_schema["properties"]["operation"]["enum"],
            [item.value for item in Level1Operation],
        )
        conditioned_operations: set[str] = set()
        for branch in request_schema["allOf"]:
            # Some branches condition on the schema version rather than on the
            # operation; only the operation branches answer this question.
            operation = branch["if"]["properties"].get("operation")
            if operation is None:
                continue
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
