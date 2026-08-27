"""Behavioral tests for record-atomic Level 1 model rendering."""

from __future__ import annotations

import copy
import unittest

from taf_context.level1_models import (
    Level1Coverage,
    Level1Finding,
    Level1Request,
    Level1ResultStatus,
)
from taf_context.level1_render import redact_preview, render_level1_result
from taf_context.models import Freshness

from .test_level1_models import INDEX_IDENTITY, finding_wire, request_wire


def request(*, budget: int = 4000, maximum_results: int = 10) -> Level1Request:
    wire = request_wire()
    wire["maximum_model_output_characters"] = budget
    wire["maximum_results"] = maximum_results
    return Level1Request.from_dict(wire)


def coverage() -> Level1Coverage:
    return Level1Coverage.from_dict(
        {
            "path_coverage": 1.0,
            "language_coverage": 1.0,
            "indexed_path_count": 20,
            "excluded_path_count": 2,
            "unsupported_language_count": 0,
            "parse_failure_count": 0,
            "exclusion_reason_counts": {"vendor": 2},
        }
    )


def finding(rank: int, *, preview: str = "class RecoveryDossier:") -> Level1Finding:
    wire = finding_wire()
    wire.update(
        {
            "rank": rank,
            "result_identity": "sha256:" + f"{rank:064x}",
            "path": f"tools/taf-context/taf_context/module_{rank:02d}.py",
            "start_line": rank * 10,
            "end_line": rank * 10 + 4,
            "qualified_name": f"taf_context.module_{rank:02d}.RecoveryDossier",
            "preview": preview,
        }
    )
    return Level1Finding.from_dict(wire)


def render(
    request_value: Level1Request,
    findings: tuple[Level1Finding, ...],
    *,
    warnings: tuple[str, ...] = (),
    provider_omitted_count: int = 0,
):
    return render_level1_result(
        request_value,
        status=Level1ResultStatus.READY,
        provider_version="0.1.0",
        index_identity=INDEX_IDENTITY,
        freshness=Freshness.EXACT,
        parser_versions=(("tree-sitter-python", "0.25.0"),),
        coverage=coverage(),
        ranked_findings=findings,
        provider_omitted_count=provider_omitted_count,
        warnings=warnings,
        next_safe_action="use-cited-evidence",
    )


class ExactRenderingTests(unittest.TestCase):
    def test_one_finding_renders_the_hand_checked_contract(self) -> None:
        rendered = render(request(), (finding(1),))

        expected = (
            "LEVEL1 status=ready operation=search-symbols freshness=exact "
            "returned=1 omitted=0 warnings=0\n"
            "COVERAGE paths=1.000 languages=1.000 unsupported=0 "
            "parse_failures=0\n"
            "FINDING verified definition "
            "tools/taf-context/taf_context/module_01.py:10-14 Python "
            "taf_context.module_01.RecoveryDossier "
            "method=tree-sitter-python@0.25.0\n"
            "PREVIEW class RecoveryDossier:\n"
            "NEXT use-cited-evidence\n"
        )
        self.assertEqual(rendered.model_text, expected)
        self.assertEqual(rendered.result.output_characters, len(expected))
        self.assertEqual(rendered.result.returned_count, 1)
        self.assertEqual(rendered.result.omitted_count, 0)
        self.assertFalse(rendered.result.truncated)
        self.assertEqual(rendered.result.findings[0].preview, "class RecoveryDossier:")

    def test_warnings_are_counted_without_copying_warning_payload_to_model_text(self) -> None:
        rendered = render(
            request(),
            (finding(1),),
            warnings=("parse-failure", "unsupported-language"),
        )

        self.assertIn("warnings=2\n", rendered.model_text)
        self.assertNotIn("parse-failure", rendered.model_text)
        self.assertNotIn("unsupported-language", rendered.model_text)
        self.assertEqual(
            rendered.result.warnings,
            ("parse-failure", "unsupported-language"),
        )


class BudgetRenderingTests(unittest.TestCase):
    def test_provider_omissions_are_preserved_without_placeholder_records(self) -> None:
        rendered = render(
            request(maximum_results=2),
            (finding(1), finding(2)),
            provider_omitted_count=7,
        )

        self.assertEqual(rendered.result.returned_count, 2)
        self.assertEqual(rendered.result.omitted_count, 7)
        self.assertTrue(rendered.result.truncated)
        self.assertIn("returned=2 omitted=7", rendered.model_text)

    def test_every_budget_is_hard_and_omission_counts_cover_all_eligible_records(self) -> None:
        findings = tuple(finding(rank, preview="π" * 500) for rank in range(1, 21))

        for budget in (2000, 4000, 8000, 12000):
            with self.subTest(budget=budget):
                rendered = render(
                    request(budget=budget, maximum_results=10),
                    findings,
                )
                self.assertLessEqual(len(rendered.model_text), budget)
                self.assertEqual(
                    rendered.result.output_characters,
                    len(rendered.model_text),
                )
                self.assertEqual(
                    rendered.result.omitted_count,
                    len(findings) - rendered.result.returned_count,
                )
                self.assertEqual(
                    rendered.result.truncated,
                    rendered.result.omitted_count > 0,
                )
                self.assertEqual(
                    [item.rank for item in rendered.result.findings],
                    list(range(1, rendered.result.returned_count + 1)),
                )

    def test_citations_are_admitted_before_optional_previews(self) -> None:
        findings = tuple(finding(rank, preview="x" * 500) for rank in range(1, 21))
        rendered = render(request(budget=2000, maximum_results=10), findings)

        preview_count = rendered.model_text.count("\nPREVIEW ")
        self.assertGreater(rendered.result.returned_count, preview_count)
        self.assertEqual(
            rendered.model_text.count("\nFINDING "),
            rendered.result.returned_count,
        )
        for line in rendered.model_text.splitlines():
            self.assertTrue(
                line.startswith(("LEVEL1 ", "COVERAGE ", "FINDING ", "PREVIEW ", "NEXT ")),
                line,
            )

    def test_a_record_that_cannot_fit_is_omitted_without_a_partial_line(self) -> None:
        huge_name_wire = finding_wire()
        huge_name_wire.update(
            {
                "rank": 1,
                "result_identity": "sha256:" + "1" * 64,
                "path": "a/" + "p" * 500,
                "language": "L" * 512,
                "qualified_name": "q" * 512,
                "extraction_method": "m" * 512,
                "preview": "p" * 512,
            }
        )
        first = Level1Finding.from_dict(huge_name_wire)
        findings = (first,) + tuple(finding(rank) for rank in range(2, 8))

        rendered = render(request(budget=2000, maximum_results=7), findings)

        self.assertNotIn("q" * 100, rendered.model_text)
        self.assertEqual(rendered.result.returned_count, 0)
        self.assertEqual(rendered.result.omitted_count, 7)
        self.assertTrue(rendered.model_text.endswith("NEXT use-cited-evidence\n"))


class RedactionAndDeterminismTests(unittest.TestCase):
    def test_redaction_removes_secrets_and_absolute_paths_only(self) -> None:
        source = (
            "token=abc123 /Users/alice/project "
            "C:\\Users\\alice\\project \\\\server\\share "
            "tools/taf-context/taf_context/recovery.py"
        )

        self.assertEqual(
            redact_preview(source),
            "token=<redacted> <absolute-path> <absolute-path> "
            "<absolute-path> tools/taf-context/taf_context/recovery.py",
        )

    def test_repeated_input_is_identical_and_noncanonical_order_is_rejected(self) -> None:
        findings = (finding(1), finding(2), finding(3))
        first = render(request(), findings)
        second = render(request(), findings)
        self.assertEqual(first, second)

        with self.assertRaises(ValueError):
            render(request(), tuple(reversed(findings)))

    def test_renderer_does_not_mutate_caller_findings(self) -> None:
        original = finding(1, preview="password=hunter2")
        snapshot = copy.deepcopy(original)

        rendered = render(request(), (original,))

        self.assertEqual(original, snapshot)
        self.assertEqual(original.preview, "password=hunter2")
        self.assertEqual(rendered.result.findings[0].preview, "password=<redacted>")


if __name__ == "__main__":
    unittest.main()
