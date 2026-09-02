"""Resolve the user-local TAF state root from explicit inputs only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Union


class StateError(ValueError):
    """A fail-closed state error identified by a stable reason code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class StatePaths:
    """The resolved state root. All state lives beneath ``root``."""

    root: Path


def resolve_state_paths(
    environment: Mapping[str, str], platform_name: str, home: Union[Path, str]
) -> StatePaths:
    """Resolve state paths solely from the explicitly supplied inputs."""
    override = environment.get("TAF_STATE_HOME")
    if "TAF_STATE_HOME" in environment:
        if not override:
            raise StateError("state-home-unavailable")
        return StatePaths(Path(override))
    if platform_name == "darwin":
        return StatePaths(Path(home) / "Library" / "Application Support" / "TAF" / "context")
    if platform_name.startswith("linux"):
        xdg = environment.get("XDG_STATE_HOME")
        base = Path(xdg) if xdg else Path(home) / ".local" / "state"
        return StatePaths(base / "taf" / "context")
    if platform_name == "win32":
        local = environment.get("LOCALAPPDATA")
        if not local:
            raise StateError("state-home-unavailable")
        return StatePaths(Path(local) / "TAF" / "context")
    raise StateError("state-home-unavailable")
