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


def register_provider_commands(*args: object, **kwargs: object) -> object:
    """Load the optional provider-control surface only when it is requested."""
    module = importlib.import_module(".provider_cli", __name__)
    return module.register_provider_commands(*args, **kwargs)


def run_provider_command(*args: object, **kwargs: object) -> object:
    """Load the optional provider-control surface only when it is requested."""
    module = importlib.import_module(".provider_cli", __name__)
    return module.run_provider_command(*args, **kwargs)

__all__ = (
    "EvidenceClass",
    "RecoveryClaim",
    "RecoveryCoverage",
    "RecoveryDossier",
    "WorkState",
    "WorkstreamState",
    "register_provider_commands",
    "run_provider_command",
)
