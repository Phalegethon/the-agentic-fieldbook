"""Bounded legacy MCP client for one newline-framed stdio tool call."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
import re
import selectors
import subprocess
import time


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PROTOCOL = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_ENVIRONMENT = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:API_?KEY|CREDENTIAL|PASSWORD|SECRET|TOKEN)(?:_|$)", re.I
)
_MAX_STDOUT = 256 * 1024
_MAX_STDERR = 64 * 1024
_MAX_CONTENT = 64 * 1024
_MAX_SCHEMA = 64 * 1024


class MCPProcessError(RuntimeError):
    """Stable reason code for a rejected MCP session."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class MCPPolicy:
    protocol_version: str
    expected_tool_schema_digest: str
    timeout_seconds: float
    maximum_stdout_bytes: int
    maximum_stderr_bytes: int
    maximum_message_bytes: int
    maximum_notifications: int
    maximum_content_characters: int
    environment: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.protocol_version, str) or _PROTOCOL.fullmatch(
            self.protocol_version
        ) is None:
            raise ValueError("protocol_version")
        if not isinstance(
            self.expected_tool_schema_digest, str
        ) or _DIGEST.fullmatch(self.expected_tool_schema_digest) is None:
            raise ValueError("expected_tool_schema_digest")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 120
        ):
            raise ValueError("timeout_seconds")
        _bounded_int(
            self.maximum_stdout_bytes, 1, _MAX_STDOUT,
            "maximum_stdout_bytes",
        )
        _bounded_int(
            self.maximum_stderr_bytes, 0, _MAX_STDERR,
            "maximum_stderr_bytes",
        )
        _bounded_int(
            self.maximum_message_bytes, 1, self.maximum_stdout_bytes,
            "maximum_message_bytes",
        )
        _bounded_int(
            self.maximum_notifications, 0, 8, "maximum_notifications"
        )
        _bounded_int(
            self.maximum_content_characters, 1, _MAX_CONTENT,
            "maximum_content_characters",
        )
        if type(self.environment) is not tuple:
            raise ValueError("environment")
        previous = ""
        for pair in self.environment:
            if (
                type(pair) is not tuple
                or len(pair) != 2
                or type(pair[0]) is not str
                or type(pair[1]) is not str
                or _ENVIRONMENT.fullmatch(pair[0]) is None
                or _SECRET_NAME.search(pair[0]) is not None
                or not len(pair[1]) <= 4096
                or "\x00" in pair[1]
                or pair[0] <= previous
            ):
                raise ValueError("environment")
            previous = pair[0]


@dataclass(frozen=True)
class MCPToolSchema:
    name: str
    description: str
    input_schema: dict[str, object]
    output_schema: dict[str, object] | None
    schema_digest: str

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "MCPToolSchema":
        if type(value) is not dict or not {
            "name", "inputSchema"
        }.issubset(value) or not set(value).issubset(
            {"name", "description", "inputSchema", "outputSchema"}
        ):
            raise ValueError("tool_schema")
        name = value["name"]
        description = value.get("description", "")
        input_schema = value["inputSchema"]
        output_schema = value.get("outputSchema")
        if type(name) is not str or _TOOL_NAME.fullmatch(name) is None:
            raise ValueError("tool_name")
        if type(description) is not str or len(description) > 4096:
            raise ValueError("tool_description")
        if type(input_schema) is not dict or (
            output_schema is not None and type(output_schema) is not dict
        ):
            raise ValueError("tool_schema")
        material = {
            "name": name,
            "inputSchema": input_schema,
            "outputSchema": output_schema,
        }
        wire = _canonical(material, "tool_schema")
        if len(wire) > _MAX_SCHEMA:
            raise ValueError("tool_schema")
        digest = "sha256:" + hashlib.sha256(wire).hexdigest()
        return cls(name, description, input_schema, output_schema, digest)


@dataclass(frozen=True)
class MCPToolResult:
    tool: str
    tool_schema: MCPToolSchema
    structured_content: dict[str, object] | None
    text_content: tuple[str, ...]
    elapsed_ns: int
    stdout_bytes: int
    stderr_bytes: int
    notification_count: int


def call_mcp_tool(
    command: tuple[str, ...],
    tool: str,
    arguments: dict[str, object],
    policy: MCPPolicy,
) -> MCPToolResult:
    """Perform one exact legacy MCP initialize/list/call stdio session."""
    _validate_command(command)
    if type(tool) is not str or _TOOL_NAME.fullmatch(tool) is None:
        raise MCPProcessError("invalid-tool")
    if type(arguments) is not dict:
        raise MCPProcessError("invalid-arguments")
    try:
        _canonical(arguments, "invalid-arguments")
    except ValueError as error:
        raise MCPProcessError("invalid-arguments") from error
    deadline = time.monotonic() + policy.timeout_seconds
    started = time.monotonic_ns()
    process: subprocess.Popen[bytes] | None = None
    session: _Session | None = None
    try:
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(policy.environment),
                start_new_session=False,
            )
        except (OSError, ValueError) as error:
            raise MCPProcessError("launch-failed") from error
        session = _Session(process, policy, deadline)
        session.send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": policy.protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "taf-context", "version": "1"},
            },
        })
        initialized = _rpc_result(session.receive(1), "initialize-error")
        _validate_initialize(initialized, policy.protocol_version)
        session.send({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        session.send({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
        })
        listed = _rpc_result(session.receive(2), "tools-list-error")
        schema = _select_tool(listed, tool, policy.expected_tool_schema_digest)
        session.send({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
        called = _rpc_result(session.receive(3), "tool-error")
        structured, texts = _parse_tool_result(
            called, policy.maximum_content_characters
        )
        session.close_cleanly()
        return MCPToolResult(
            tool,
            schema,
            structured,
            texts,
            time.monotonic_ns() - started,
            session.stdout_bytes,
            session.stderr_bytes,
            session.notifications,
        )
    finally:
        if session is not None:
            session.close()
        elif process is not None and process.poll() is None:
            _kill(process)


class _Session:
    def __init__(
        self, process: subprocess.Popen[bytes], policy: MCPPolicy, deadline: float
    ) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise MCPProcessError("launch-failed")
        self.process = process
        self.policy = policy
        self.deadline = deadline
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.selector = selectors.DefaultSelector()
        os.set_blocking(self.stdout.fileno(), False)
        os.set_blocking(self.stderr.fileno(), False)
        self.selector.register(self.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.stderr, selectors.EVENT_READ, "stderr")
        self.buffer = bytearray()
        self.stdout_bytes = 0
        self.stderr_bytes = 0
        self.notifications = 0

    def send(self, value: dict[str, object]) -> None:
        wire = _canonical(value, "invalid-client-message") + b"\n"
        if len(wire) > self.policy.maximum_message_bytes:
            raise MCPProcessError("client-message-budget-exceeded")
        try:
            self.stdin.write(wire)
            self.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise MCPProcessError("early-exit") from error

    def receive(self, expected_id: int) -> dict[str, object]:
        while True:
            message = self._next_message()
            if message.get("jsonrpc") != "2.0":
                raise MCPProcessError("invalid-jsonrpc")
            has_method = "method" in message
            has_id = "id" in message
            if has_method and has_id:
                raise MCPProcessError("server-request-unsupported")
            if has_method:
                if (
                    type(message["method"]) is not str
                    or not set(message).issubset(
                        {"jsonrpc", "method", "params"}
                    )
                ):
                    raise MCPProcessError("invalid-jsonrpc")
                self.notifications += 1
                if self.notifications > self.policy.maximum_notifications:
                    raise MCPProcessError("notification-budget-exceeded")
                continue
            if (
                not has_id
                or type(message["id"]) is not int
                or message["id"] != expected_id
            ):
                raise MCPProcessError("unexpected-response-id")
            if ("result" in message) == ("error" in message):
                raise MCPProcessError("invalid-jsonrpc")
            if not set(message).issubset({"jsonrpc", "id", "result", "error"}):
                raise MCPProcessError("invalid-jsonrpc")
            return message

    def _next_message(self) -> dict[str, object]:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                if not raw or len(raw) > self.policy.maximum_message_bytes:
                    raise MCPProcessError("message-budget-exceeded")
                return _parse_message(raw)
            if len(self.buffer) > self.policy.maximum_message_bytes:
                raise MCPProcessError("message-budget-exceeded")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise MCPProcessError("timeout")
            events = self.selector.select(remaining)
            if not events:
                raise MCPProcessError("timeout")
            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    self.selector.unregister(key.fileobj)
                    if key.data == "stdout":
                        if self.buffer:
                            raise MCPProcessError("partial-frame")
                        raise MCPProcessError("early-exit")
                    continue
                if key.data == "stdout":
                    self.stdout_bytes += len(chunk)
                    if self.stdout_bytes > self.policy.maximum_stdout_bytes:
                        raise MCPProcessError("stdout-budget-exceeded")
                    self.buffer.extend(chunk)
                else:
                    self.stderr_bytes += len(chunk)
                    if self.stderr_bytes > self.policy.maximum_stderr_bytes:
                        raise MCPProcessError("stderr-budget-exceeded")

    def close_cleanly(self) -> None:
        try:
            self.stdin.close()
        except OSError:
            pass
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise MCPProcessError("timeout")
        try:
            return_code = self.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            raise MCPProcessError("termination-timeout") from error
        self._drain_after_exit()
        if return_code != 0:
            raise MCPProcessError("nonzero-exit")

    def _drain_after_exit(self) -> None:
        stdout = self.stdout.read() or b""
        stderr = self.stderr.read() or b""
        self.stdout_bytes += len(stdout)
        self.stderr_bytes += len(stderr)
        if self.stdout_bytes > self.policy.maximum_stdout_bytes:
            raise MCPProcessError("stdout-budget-exceeded")
        if self.stderr_bytes > self.policy.maximum_stderr_bytes:
            raise MCPProcessError("stderr-budget-exceeded")
        if stdout or self.buffer:
            raise MCPProcessError("unexpected-output")

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is None:
            _kill(self.process)
        for stream in (self.stdin, self.stdout, self.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _validate_command(command: tuple[str, ...]) -> None:
    if (
        type(command) is not tuple
        or not command
        or len(command) > 64
        or any(
            type(item) is not str
            or not item
            or len(item) > 4096
            or "\x00" in item
            for item in command
        )
        or not os.path.isabs(command[0])
    ):
        raise MCPProcessError("invalid-command")


def _parse_message(raw: bytes) -> dict[str, object]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MCPProcessError("invalid-utf8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise MCPProcessError("invalid-json") from error
    if type(value) is not dict:
        raise MCPProcessError("invalid-jsonrpc")
    return value


def _rpc_result(message: dict[str, object], error_code: str) -> dict[str, object]:
    if "error" in message:
        raise MCPProcessError(error_code)
    result = message["result"]
    if type(result) is not dict:
        raise MCPProcessError("invalid-jsonrpc")
    return result


def _validate_initialize(value: dict[str, object], protocol: str) -> None:
    if value.get("protocolVersion") != protocol:
        raise MCPProcessError("protocol-version-mismatch")
    capabilities = value.get("capabilities")
    server_info = value.get("serverInfo")
    if (
        type(capabilities) is not dict
        or type(capabilities.get("tools")) is not dict
        or type(server_info) is not dict
    ):
        raise MCPProcessError("invalid-initialize-result")
    if (
        type(server_info.get("name")) is not str
        or type(server_info.get("version")) is not str
    ):
        raise MCPProcessError("invalid-initialize-result")


def _select_tool(
    value: dict[str, object], tool: str, expected_digest: str
) -> MCPToolSchema:
    if "nextCursor" in value:
        raise MCPProcessError("tool-pagination-unsupported")
    if (
        set(value) != {"tools"}
        or type(value["tools"]) is not list
        or len(value["tools"]) > 64
    ):
        raise MCPProcessError("invalid-tools-list")
    schemas: dict[str, MCPToolSchema] = {}
    try:
        for item in value["tools"]:
            schema = MCPToolSchema.from_dict(item)
            if schema.name in schemas:
                raise MCPProcessError("duplicate-tool")
            schemas[schema.name] = schema
    except (TypeError, ValueError) as error:
        raise MCPProcessError("invalid-tool-schema") from error
    if tool not in schemas:
        raise MCPProcessError("tool-unavailable")
    selected = schemas[tool]
    if selected.schema_digest != expected_digest:
        raise MCPProcessError("tool-schema-mismatch")
    return selected


def _parse_tool_result(
    value: dict[str, object], maximum_characters: int
) -> tuple[dict[str, object] | None, tuple[str, ...]]:
    if not set(value).issubset({"content", "structuredContent", "isError", "_meta"}):
        raise MCPProcessError("invalid-tool-result")
    is_error = value.get("isError", False)
    if type(is_error) is not bool:
        raise MCPProcessError("invalid-tool-result")
    if is_error:
        raise MCPProcessError("tool-error")
    structured = value.get("structuredContent")
    if structured is not None and type(structured) is not dict:
        raise MCPProcessError("invalid-tool-result")
    content = value.get("content", [])
    if type(content) is not list or len(content) > 64:
        raise MCPProcessError("invalid-tool-result")
    texts = []
    for item in content:
        if type(item) is not dict or set(item) != {"type", "text"}:
            raise MCPProcessError("unsupported-content")
        if item["type"] != "text" or type(item["text"]) is not str:
            raise MCPProcessError("unsupported-content")
        texts.append(item["text"])
    try:
        structured_characters = (
            len(_canonical(structured, "invalid-tool-result").decode("utf-8"))
            if structured is not None
            else 0
        )
    except ValueError as error:
        raise MCPProcessError("invalid-tool-result") from error
    if structured_characters + sum(len(item) for item in texts) > maximum_characters:
        raise MCPProcessError("content-budget-exceeded")
    if structured is None and not texts:
        raise MCPProcessError("empty-tool-result")
    return structured, tuple(texts)


def _canonical(value: object, reason: str) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError(reason) from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def _bounded_int(value: object, minimum: int, maximum: int, field: str) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(field)


def _kill(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
