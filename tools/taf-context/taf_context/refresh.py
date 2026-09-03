"""Incremental index refresh: binding memory, change deltas, and the change document."""

from __future__ import annotations

from dataclasses import dataclass


MAXIMUM_BINDING_DIRTY_PATHS = 5000


@dataclass(frozen=True)
class Binding:
    """What the bound index was built against; schema-1 bindings have no delta inputs."""

    index_identity: str
    head_sha: str | None
    dirty_fingerprint: str | None
    dirty_paths: tuple[str, ...] | None

    @property
    def has_delta_inputs(self) -> bool:
        return (
            self.head_sha is not None
            and self.dirty_fingerprint is not None
            and self.dirty_paths is not None
        )


def dirty_paths_of(snapshot: object) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(snapshot.staged_paths) | set(snapshot.unstaged_paths) | set(snapshot.untracked_paths)
        )
    )
