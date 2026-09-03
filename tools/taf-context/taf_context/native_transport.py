"""Process boundary between the broker and the native Level 1 engine."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Protocol


DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0
"""The deadline every transport gives one engine request."""


class NativeTransportError(RuntimeError):
    """The engine could not be reached or refused; ``reason`` is a stable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


class NativeTransport(Protocol):
    def exchange(self, wire: bytes, *, idempotent: bool) -> bytes:
        """Send one framed request and return one framed result."""


class OneShotTransport:
    """One engine process per request: the broker's original behaviour."""

    def __init__(
        self, binary: Path, *, timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds

    def exchange(self, wire: bytes, *, idempotent: bool) -> bytes:
        del idempotent  # a fresh process has nothing to retry
        try:
            completed = subprocess.run(
                [str(self._binary)],
                input=wire,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise NativeTransportError("timeout") from exc
        except OSError as exc:
            raise NativeTransportError("invocation-failed") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()[:200]
            raise NativeTransportError("rejected", detail)
        return completed.stdout
