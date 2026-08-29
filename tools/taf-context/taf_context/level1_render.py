"""Record-atomic model rendering for bounded Level 1 evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .level1_models import (
    Level1Coverage,
    Level1Finding,
    Level1Operation,
    Level1Request,
    Level1Result,
    Level1ResultStatus,
)
from .models import Freshness


_SECRET = re.compile(
    r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+"
)
_WINDOWS_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:\\|\\\\)[^\s]+")
_POSIX_ABSOLUTE = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:Users|home|private|tmp|var|opt|etc)/[^\s]+"
)


@dataclass(frozen=True)
class RenderedLevel1Result:
    result: Level1Result
    model_text: str


def redact_preview(text: str) -> str:
    """Redact deterministic secret assignments and machine-absolute paths."""
    redacted = _SECRET.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        text,
    )
    redacted = _WINDOWS_ABSOLUTE.sub("<absolute-path>", redacted)
    return _POSIX_ABSOLUTE.sub("<absolute-path>", redacted)


def render_level1_result(
    request: Level1Request,
    *,
    status: Level1ResultStatus,
    provider_version: str,
    index_identity: str | None,
    freshness: Freshness,
    parser_versions: tuple[tuple[str, str], ...],
    coverage: Level1Coverage,
    ranked_findings: tuple[Level1Finding, ...],
    warnings: tuple[str, ...],
    next_safe_action: str,
    provider_omitted_count: int = 0,
) -> RenderedLevel1Result:
    """Render a deterministic prefix without splitting a record or citation."""
    _validate_ranked_findings(ranked_findings)
    if type(provider_omitted_count) is not int or provider_omitted_count < 0:
        raise ValueError("invalid provider_omitted_count")
    _validate_sorted_pairs(parser_versions, "parser_versions")
    _validate_sorted_values(warnings, "warnings")

    maximum = min(request.maximum_results, len(ranked_findings))
    mandatory_previews = request.operation is Level1Operation.SOURCE_SNIPPETS
    redacted_previews = tuple(
        redact_preview(item.preview) for item in ranked_findings[:maximum]
    ) if mandatory_previews else ()
    selected_count = 0
    for count in range(0, maximum + 1):
        candidate = tuple(
            _finding_with_preview(
                item,
                redacted_previews[index] if mandatory_previews else "",
            )
            for index, item in enumerate(ranked_findings[:count])
        )
        text = _render_text(
            request,
            status,
            freshness,
            coverage,
            candidate,
            provider_omitted_count + len(ranked_findings) - count,
            len(warnings),
            next_safe_action,
        )
        if len(text) > request.maximum_model_output_characters:
            break
        selected_count = count

    selected = tuple(
        _finding_with_preview(
            item,
            redacted_previews[index] if mandatory_previews else "",
        )
        for index, item in enumerate(ranked_findings[:selected_count])
    )
    omitted_count = (
        provider_omitted_count + len(ranked_findings) - selected_count
    )
    base_text = _render_text(
        request,
        status,
        freshness,
        coverage,
        selected,
        omitted_count,
        len(warnings),
        next_safe_action,
    )
    if len(base_text) > request.maximum_model_output_characters:
        raise ValueError("mandatory Level 1 metadata exceeds request budget")

    mutable = list(selected)
    if not mandatory_previews:
        for index, original in enumerate(ranked_findings[:selected_count]):
            preview = redact_preview(original.preview)
            if not preview:
                continue
            proposed = list(mutable)
            proposed[index] = _finding_with_preview(original, preview)
            proposed_text = _render_text(
                request,
                status,
                freshness,
                coverage,
                tuple(proposed),
                omitted_count,
                len(warnings),
                next_safe_action,
            )
            if len(proposed_text) <= request.maximum_model_output_characters:
                mutable = proposed

    final_findings = tuple(mutable)
    model_text = _render_text(
        request,
        status,
        freshness,
        coverage,
        final_findings,
        omitted_count,
        len(warnings),
        next_safe_action,
    )
    wire = {
        "schema_version": "1",
        "request_identity": request.request_identity,
        "operation": request.operation.value,
        "status": status.value,
        "provider_identity": request.provider_identity,
        "provider_version": provider_version,
        "index_identity": index_identity,
        "repository_identity": request.repository_identity,
        "worktree_identity": request.worktree_identity,
        "committed_head": request.committed_head,
        "dirty_overlay_fingerprint": request.dirty_overlay_fingerprint,
        "freshness": freshness.value,
        "parser_versions": dict(parser_versions),
        "coverage": coverage.to_dict(),
        "findings": [item.to_dict() for item in final_findings],
        "returned_count": len(final_findings),
        "omitted_count": omitted_count,
        "truncated": omitted_count > 0,
        "output_characters": len(model_text),
        "warnings": list(warnings),
        "next_safe_action": next_safe_action,
    }
    return RenderedLevel1Result(Level1Result.from_dict(wire), model_text)


def _render_text(
    request: Level1Request,
    status: Level1ResultStatus,
    freshness: Freshness,
    coverage: Level1Coverage,
    findings: tuple[Level1Finding, ...],
    omitted_count: int,
    warning_count: int,
    next_safe_action: str,
) -> str:
    lines = [
        (
            f"LEVEL1 status={status.value} operation={request.operation.value} "
            f"freshness={freshness.value} returned={len(findings)} "
            f"omitted={omitted_count} warnings={warning_count}"
        ),
        (
            f"COVERAGE paths={coverage.path_coverage:.3f} "
            f"languages={coverage.language_coverage:.3f} "
            f"unsupported={coverage.unsupported_language_count} "
            f"parse_failures={coverage.parse_failure_count}"
        ),
    ]
    for finding in findings:
        lines.append(
            f"FINDING {finding.evidence_class.value} "
            f"{finding.record_kind.value} {finding.path}:"
            f"{finding.start_line}-{finding.end_line} {finding.language} "
            f"{finding.qualified_name} method={finding.extraction_method}"
        )
        if finding.preview or request.operation is Level1Operation.SOURCE_SNIPPETS:
            lines.extend(f"PREVIEW {line}" for line in finding.preview.split("\n"))
    lines.append(f"NEXT {next_safe_action}")
    return "\n".join(lines) + "\n"


def _finding_with_preview(
    finding: Level1Finding,
    preview: str,
) -> Level1Finding:
    wire = finding.to_dict()
    wire["preview"] = preview
    return Level1Finding.from_dict(wire)


def _validate_ranked_findings(
    findings: tuple[Level1Finding, ...],
) -> None:
    if type(findings) is not tuple or len(findings) > 64:
        raise ValueError("invalid ranked findings")
    if tuple(item.rank for item in findings) != tuple(
        range(1, len(findings) + 1)
    ):
        raise ValueError("invalid ranked findings")
    identities = tuple(item.result_identity for item in findings)
    if len(set(identities)) != len(identities):
        raise ValueError("invalid ranked findings")


def _validate_sorted_pairs(
    items: tuple[tuple[str, str], ...],
    field: str,
) -> None:
    if type(items) is not tuple or items != tuple(sorted(items)):
        raise ValueError(f"invalid {field}")
    keys = tuple(key for key, _value in items)
    if len(set(keys)) != len(keys):
        raise ValueError(f"invalid {field}")


def _validate_sorted_values(items: tuple[str, ...], field: str) -> None:
    if (
        type(items) is not tuple
        or items != tuple(sorted(items))
        or len(set(items)) != len(items)
    ):
        raise ValueError(f"invalid {field}")
