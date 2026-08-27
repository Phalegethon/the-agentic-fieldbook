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
from .level1_models import (
    CandidateAvailability,
    CandidateManifest,
    Level1Coverage,
    Level1Filters,
    Level1Finding,
    Level1Operation,
    Level1RecordKind,
    Level1Request,
    Level1Result,
    Level1ResultStatus,
    Level1SourceType,
    parse_level1_request,
    parse_level1_result,
)


def register_provider_commands(*args: object, **kwargs: object) -> object:
    """Load the optional provider-control surface only when it is requested."""
    module = importlib.import_module(".provider_cli", __name__)
    return module.register_provider_commands(*args, **kwargs)


def run_provider_command(*args: object, **kwargs: object) -> object:
    """Load the optional provider-control surface only when it is requested."""
    module = importlib.import_module(".provider_cli", __name__)
    return module.run_provider_command(*args, **kwargs)

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
    "register_provider_commands",
    "run_provider_command",
)
