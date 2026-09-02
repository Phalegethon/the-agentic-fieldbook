"""Provider-independent records for TAF context tooling."""

import importlib

from .recovery_models import (
    EvidenceClass,
    RecoveryClaim,
    RecoveryCoverage,
    RecoveryDossier,
    WorkState,
    WorkstreamState,
)
_LEVEL1_EXPORTS = frozenset(
    {
        "CandidateAvailability",
        "CandidateManifest",
        "Level1Coverage",
        "Level1Filters",
        "Level1Finding",
        "Level1Operation",
        "Level1RecordKind",
        "Level1Request",
        "Level1Result",
        "Level1ResultStatus",
        "Level1SourceType",
        "parse_level1_request",
        "parse_level1_result",
    }
)


def __getattr__(name: str) -> object:
    """Load the optional Level 1 contract surface only when requested."""
    if name not in _LEVEL1_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(".level1_models", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


__all__ = (
    "CandidateAvailability",
    "CandidateManifest",
    "EvidenceClass",
    "Level1Coverage",
    "Level1Filters",
    "Level1Finding",
    "Level1Operation",
    "Level1RecordKind",
    "Level1Request",
    "Level1Result",
    "Level1ResultStatus",
    "Level1SourceType",
    "RecoveryClaim",
    "RecoveryCoverage",
    "RecoveryDossier",
    "WorkState",
    "WorkstreamState",
    "parse_level1_request",
    "parse_level1_result",
)
