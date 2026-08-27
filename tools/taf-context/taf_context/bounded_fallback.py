"""Index-free, candidate-first Level 1 fallback."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .level1_models import (
    Level1Coverage,
    Level1Finding,
    Level1Operation,
    Level1RecordKind,
    Level1Request,
    Level1Result,
    Level1ResultStatus,
    Level1SourceType,
)
from .level1_render import redact_preview, render_level1_result
from .models import Confidence, Freshness


_EXCLUDED_PARTS = frozenset({".git", "node_modules", "vendor", "vendors", "generated", "dist", "build"})
_SYMBOL = re.compile(r"^\s*(?:class|def|function|func|struct|enum|interface|type)\s+([A-Za-z_][A-Za-z0-9_]*)")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class FallbackPolicy:
    maximum_files: int
    maximum_bytes: int
    maximum_file_bytes: int
    maximum_candidate_paths: int

    def __post_init__(self) -> None:
        values = (
            self.maximum_files, self.maximum_bytes,
            self.maximum_file_bytes, self.maximum_candidate_paths,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("invalid-fallback-policy")
        if self.maximum_file_bytes > self.maximum_bytes:
            raise ValueError("invalid-fallback-policy")


@dataclass(frozen=True)
class FallbackEvidence:
    candidate_paths: int
    files_read: int
    bytes_read: int
    excluded_paths: int
    budget_exhausted: bool


def run_bounded_fallback(
    request: Level1Request,
    repository_root: Path,
    candidate_paths: tuple[str, ...],
    policy: FallbackPolicy,
) -> tuple[Level1Result, FallbackEvidence]:
    """Answer one lexical request without creating persistent repository state."""
    if request.provider_identity != "taf.bounded-fallback":
        raise ValueError("fallback-provider-identity-mismatch")
    if request.operation not in {
        Level1Operation.SEARCH_SYMBOLS,
        Level1Operation.SEARCH_DOCS,
        Level1Operation.REPOSITORY_MAP,
    }:
        rendered = _render(request, (), 0, len(candidate_paths), True, ("fallback-capability-unsupported",))
        return rendered, FallbackEvidence(0, 0, 0, len(candidate_paths), False)
    root = repository_root.resolve()
    selected = tuple(sorted(set(candidate_paths)))[: policy.maximum_candidate_paths]
    query = (request.query or "").casefold()
    findings: list[Level1Finding] = []
    files_read = bytes_read = excluded = 0
    exhausted = len(candidate_paths) > len(selected)
    for relative in selected:
        if files_read >= policy.maximum_files or bytes_read >= policy.maximum_bytes:
            exhausted = True
            break
        if not _allowed_path(relative, request) or any(part.casefold() in _EXCLUDED_PARTS for part in Path(relative).parts):
            excluded += 1
            continue
        lexical = root.joinpath(*relative.split("/"))
        try:
            if lexical.is_symlink():
                excluded += 1
                continue
            resolved = lexical.resolve(strict=True)
            metadata = resolved.stat()
        except (OSError, RuntimeError):
            excluded += 1
            continue
        if root not in resolved.parents or not resolved.is_file():
            excluded += 1
            continue
        if metadata.st_size > policy.maximum_file_bytes:
            excluded += 1
            exhausted = True
            continue
        remaining = policy.maximum_bytes - bytes_read
        raw = resolved.read_bytes()[: min(policy.maximum_file_bytes, remaining) + 1]
        if len(raw) > remaining or b"\x00" in raw:
            excluded += 1
            exhausted = exhausted or len(raw) > remaining
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            excluded += 1
            continue
        files_read += 1
        bytes_read += len(raw)
        for line_number, line in enumerate(text.splitlines(), 1):
            match = _HEADING.match(line) if request.operation is Level1Operation.SEARCH_DOCS else _SYMBOL.match(line)
            if request.operation is Level1Operation.REPOSITORY_MAP:
                match = None
            if match is None:
                continue
            name = match.group(2 if request.operation is Level1Operation.SEARCH_DOCS else 1).strip()
            if query and query not in name.casefold():
                continue
            kind = Level1RecordKind.HEADING if request.operation is Level1Operation.SEARCH_DOCS else Level1RecordKind.DEFINITION
            source = Level1SourceType.DOCUMENT if kind is Level1RecordKind.HEADING else Level1SourceType.SOURCE
            identity = "sha256:" + hashlib.sha256(f"{relative}:{line_number}:{name}".encode("utf-8")).hexdigest()
            findings.append(Level1Finding(len(findings) + 1, identity, relative, line_number, line_number, _language(relative), kind, source, name, "bounded-lexical-v1", Confidence.UNCERTAIN, redact_preview(line.strip())))
            if len(findings) >= request.maximum_results:
                break
        if len(findings) >= request.maximum_results:
            break
    warnings = ("fallback-budget-exhausted",) if exhausted else ()
    result = _render(request, tuple(findings), files_read, len(selected), exhausted, warnings)
    return result, FallbackEvidence(len(selected), files_read, bytes_read, excluded, exhausted)


def _render(request: Level1Request, findings: tuple[Level1Finding, ...], files_read: int, candidates: int, exhausted: bool, warnings: tuple[str, ...]) -> Level1Result:
    coverage = Level1Coverage(
        path_coverage=(files_read / candidates if candidates else 0.0),
        language_coverage=1.0 if files_read else 0.0,
        indexed_path_count=files_read,
        excluded_path_count=max(0, candidates - files_read),
        unsupported_language_count=0,
        parse_failure_count=0,
        exclusion_reason_counts=(),
    )
    rendered = render_level1_result(
        request, status=Level1ResultStatus.PARTIAL,
        provider_version="1.0.0", index_identity=request.index_identity,
        freshness=Freshness.PARTIAL, parser_versions=(("bounded-lexical", "1"),),
        coverage=coverage, ranked_findings=findings,
        warnings=warnings, next_safe_action=("review-bounded-evidence" if findings else "request-context-preparation"),
    )
    return rendered.result


def _allowed_path(relative: str, request: Level1Request) -> bool:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative:
        return False
    prefixes = request.filters.path_prefixes
    return not prefixes or any(relative == prefix or relative.startswith(prefix + "/") for prefix in prefixes)


def _language(path: str) -> str:
    return {".py": "Python", ".ts": "TypeScript", ".js": "JavaScript", ".go": "Go", ".rs": "Rust", ".md": "Markdown"}.get(Path(path).suffix.lower(), "Text")
