"""Test package root. Pins TAF_STATE_HOME so tests never touch real user state."""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import shutil
import tempfile

_GUARD_DIRECTORY: Path | None = None


def install_state_home_guard() -> Path:
    """Point TAF_STATE_HOME at a throwaway directory for this process."""
    global _GUARD_DIRECTORY
    if _GUARD_DIRECTORY is None:
        _GUARD_DIRECTORY = Path(tempfile.mkdtemp(prefix="taf-test-state-"))
        os.environ["TAF_STATE_HOME"] = str(_GUARD_DIRECTORY)
        atexit.register(shutil.rmtree, _GUARD_DIRECTORY, True)
    return _GUARD_DIRECTORY


install_state_home_guard()
