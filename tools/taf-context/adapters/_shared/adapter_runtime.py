"""Small bounded runtime shared by named local provider adapters."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
import re
import selectors
import subprocess
import time


MAX_ENVELOPE_BYTES = 256 * 1024
MAX_CHILD_STDOUT_BYTES = 256 * 1024
MAX_CHILD_STDERR_BYTES = 64 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AdapterRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderCommand:
    executable: str
    executable_digest: str
    arguments: tuple[str, ...]
    state_roots: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    binding_digest: str
    transport: str


def read_envelope() -> dict[str, object]:
    chunks = []
    total = 0
    while total <= MAX_ENVELOPE_BYTES:
        chunk = os.read(0, min(8192, MAX_ENVELOPE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    raw = b"".join(chunks)
    if (
        len(raw) > MAX_ENVELOPE_BYTES
        or not raw.endswith(b"\n")
        or raw.count(b"\n") != 1
    ):
        raise AdapterRuntimeError("invalid-envelope")
    return parse_object(raw[:-1], "invalid-envelope")


def provider_command(
    envelope: dict[str, object], expected_transport: str
) -> ProviderCommand:
    value = envelope.get("provider_command")
    expected = {
        "executable", "executable_digest", "arguments", "state_roots", "environment",
        "binding_digest", "transport",
    }
    if type(value) is not dict or set(value) != expected:
        raise AdapterRuntimeError("invalid-provider-command")
    executable = value["executable"]
    executable_digest = value["executable_digest"]
    arguments = value["arguments"]
    state_roots = value["state_roots"]
    environment = value["environment"]
    digest = value["binding_digest"]
    transport = value["transport"]
    if (
        type(executable) is not str
        or not os.path.isabs(executable)
        or type(executable_digest) is not str
        or _DIGEST.fullmatch(executable_digest) is None
        or type(arguments) is not list
        or any(type(item) is not str for item in arguments)
        or type(state_roots) is not list
        or any(type(item) is not str or not os.path.isabs(item) for item in state_roots)
        or type(environment) is not dict
        or any(type(key) is not str or type(item) is not str for key, item in environment.items())
        or type(digest) is not str
        or type(transport) is not str
        or transport != expected_transport
    ):
        raise AdapterRuntimeError("invalid-provider-command")
    return ProviderCommand(
        executable,
        executable_digest,
        tuple(arguments),
        tuple(state_roots),
        tuple(sorted(environment.items())),
        digest,
        transport,
    )


def run_child(
    command: ProviderCommand,
    extra_arguments: tuple[str, ...],
    *,
    input_value: dict[str, object] | None = None,
    timeout_seconds: float = 5.0,
) -> bytes:
    payload = b"" if input_value is None else canonical(input_value) + b"\n"
    process = subprocess.Popen(
        [command.executable, *command.arguments, *extra_arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(command.environment),
        start_new_session=False,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        raise AdapterRuntimeError("provider-launch-failed")
    try:
        process.stdin.write(payload)
        process.stdin.close()
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout = bytearray()
        stderr_bytes = 0
        deadline = time.monotonic() + timeout_seconds
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterRuntimeError("provider-timeout")
            events = selector.select(remaining)
            if not events:
                raise AdapterRuntimeError("provider-timeout")
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == "stdout":
                    stdout.extend(chunk)
                    if len(stdout) > MAX_CHILD_STDOUT_BYTES:
                        raise AdapterRuntimeError("provider-stdout-oversized")
                else:
                    stderr_bytes += len(chunk)
                    if stderr_bytes > MAX_CHILD_STDERR_BYTES:
                        raise AdapterRuntimeError("provider-stderr-oversized")
        selector.close()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterRuntimeError("provider-timeout")
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise AdapterRuntimeError("provider-timeout") from error
        if return_code != 0:
            raise AdapterRuntimeError("provider-nonzero")
        if not stdout.endswith(b"\n") or stdout.count(b"\n") != 1:
            raise AdapterRuntimeError("invalid-provider-framing")
        return bytes(stdout[:-1])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def run_json_tool(
    command: ProviderCommand, tool: str, arguments: dict[str, object]
) -> dict[str, object]:
    raw = run_child(
        command, ("cli", "--json", tool), input_value=arguments
    )
    envelope = parse_object(raw, "invalid-provider-json")
    if not set(envelope).issubset({"content", "isError", "_meta"}):
        raise AdapterRuntimeError("invalid-provider-json")
    if type(envelope.get("isError", False)) is not bool:
        raise AdapterRuntimeError("invalid-provider-json")
    if envelope.get("isError", False):
        raise AdapterRuntimeError("provider-tool-error")
    content = envelope.get("content")
    if type(content) is not list or len(content) != 1:
        raise AdapterRuntimeError("invalid-provider-json")
    item = content[0]
    if type(item) is not dict or set(item) != {"type", "text"}:
        raise AdapterRuntimeError("invalid-provider-json")
    if item["type"] != "text" or type(item["text"]) is not str:
        raise AdapterRuntimeError("invalid-provider-json")
    return parse_object(item["text"].encode("utf-8"), "invalid-provider-json")


def parse_object(raw: bytes, reason: str) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise AdapterRuntimeError(reason) from error
    if type(value) is not dict:
        raise AdapterRuntimeError(reason)
    return value


def canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AdapterRuntimeError("invalid-json-value") from error


def write_result(value: dict[str, object]) -> None:
    os.write(1, canonical(value) + b"\n")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate-key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    if not math.isfinite(float(value)):
        raise ValueError("nonfinite")
    raise ValueError("constant")
