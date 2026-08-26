#!/usr/bin/env python3
"""Deterministic fail-closed benchmark for passive discovery and routing.

Corpus construction and production-module import occur before the measured
interval.  Each retained sample runs canonical, reversed, and seed-permuted
equivalents under a guard that forbids filesystem I/O, child processes,
provider execution, Git, sockets, DNS, network-capable commands, and LLMs.
"""

from __future__ import annotations

import argparse
import builtins
import contextlib
from dataclasses import dataclass
import hashlib
import _io
import io
import json
import math
import mmap as mmap_module
import os
from pathlib import Path
import platform
import random
import re
import resource
import socket
import _socket
import subprocess
import sys
import threading
import time
from typing import Dict, Iterator, Mapping, Sequence

from taf_context.consent import AuthorizationLedger, ConsentDisposition
from taf_context.discovery import discover_providers
from taf_context.models import (
    Confidence,
    ContextAction,
    Freshness,
    RepositorySnapshot,
    canonical_json,
)
from taf_context.provider_models import (
    Availability,
    BrokerRequest,
    ConsentRequest,
    DiscoverySnapshot,
    DiscoverySource,
    HostInventory,
    ProjectRegistration,
    ProjectRegistrationEntry,
    ProviderDescriptor,
    ProviderLocality,
    Registration,
    RoutingDecision,
    StatusEvidence,
)
from taf_context.routing import route_provider


SCHEMA = "taf-context-discovery-benchmark/1"
SEED = 20260826
WARM_UP_RUNS = 1
MEASURED_RUNS = 5
WORKER_TIMEOUT_SECONDS = 30
MAX_TIMING_SECONDS = WORKER_TIMEOUT_SECONDS * 2
MAX_WORKER_OUTPUT_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 2048
MAX_INTEGER = (1 << 63) - 1
MAX_MODEL_SUMMARY_CHARACTERS = 2000
MAX_MACHINE_DECISION_BYTES = 16 * 1024
MAX_DISCOVERY_ARTIFACT_BYTES = 256 * 1024
MAX_HOST_INVENTORY_BYTES = 256 * 1024
UTC_NOW = "2026-08-26T12:00:00Z"

TIMING_DEFINITIONS = {
    "cold": "fresh worker process including interpreter, harness import, corpus construction, guard setup, measurement, validation, and serialization",
    "warm": "worker timer excludes production import, deterministic corpus construction, and guard setup; includes canonical, reversed, and seeded-permuted in-memory discovery, routing, strict round trips, and serialization",
    "routing": "one canonical route_provider call over the completed discovery snapshot",
    "consent_lookup_decision": "a repeated canonical route_provider call including exact AuthorizationLedger decision lookups",
}

THRESHOLDS = {
    "routing_64x64_warm_p95_seconds": 0.050,
    "consent_lookup_decision_warm_p95_seconds": 0.010,
    "model_summary_characters": MAX_MODEL_SUMMARY_CHARACTERS,
    "machine_decision_bytes": MAX_MACHINE_DECISION_BYTES,
    "host_inventory_bytes": MAX_HOST_INVENTORY_BYTES,
    "discovery_artifact_bytes": MAX_DISCOVERY_ARTIFACT_BYTES,
    "source_bytes": 0,
    "provider_processes": 0,
    "git_processes": 0,
    "network_calls": 0,
    "llm_calls": 0,
    "native_bypasses": 0,
    "retained_samples_per_class": MEASURED_RUNS,
}

CASES = (
    {
        "name": "1x2-native-only",
        "provider_count": 1,
        "capabilities_per_provider": 2,
        "scenario": "native-only",
        "expected_rejected_provider_count": 0,
        "routing_warm_p95_limit_seconds": None,
        "consent_decision_warm_p95_limit_seconds": 0.010,
    },
    {
        "name": "16x16-mixed-consent-freshness",
        "provider_count": 16,
        "capabilities_per_provider": 16,
        "scenario": "mixed-consent-freshness",
        "expected_rejected_provider_count": 0,
        "routing_warm_p95_limit_seconds": None,
        "consent_decision_warm_p95_limit_seconds": 0.010,
    },
    {
        "name": "64x64-conflicts-denials-network-markers",
        "provider_count": 64,
        "capabilities_per_provider": 64,
        "scenario": "conflicts-denials-network-markers",
        "expected_rejected_provider_count": 2,
        "routing_warm_p95_limit_seconds": 0.050,
        "consent_decision_warm_p95_limit_seconds": 0.010,
    },
    {
        "name": "65x64-overflow",
        "provider_count": 65,
        "capabilities_per_provider": 64,
        "scenario": "overflow-exact-omission",
        "expected_rejected_provider_count": 0,
        "routing_warm_p95_limit_seconds": None,
        "consent_decision_warm_p95_limit_seconds": 0.010,
    },
)
_CASE_INTEGER_FIELDS = (
    "provider_count",
    "capabilities_per_provider",
    "expected_rejected_provider_count",
)

CHECK_NAMES = (
    "corpus_provider_count_exact",
    "source_descriptor_count_exact",
    "capabilities_per_external_provider_exact",
    "discovered_descriptor_count_exact",
    "discovered_capability_count_exact",
    "rejected_provider_count_exact",
    "omitted_provider_count_exact",
    "host_inventory_within_256_kib",
    "model_summary_within_2000_characters",
    "machine_decision_within_16_kib",
    "discovery_artifact_within_256_kib",
    "discovery_wire_round_trip",
    "routing_wire_round_trip",
    "repeat_routing_byte_identical",
    "reversed_discovery_byte_identical",
    "reversed_routing_byte_identical",
    "permuted_discovery_byte_identical",
    "permuted_routing_byte_identical",
    "zero_source_reads",
    "zero_provider_processes",
    "zero_git_processes",
    "zero_network_calls",
    "zero_llm_calls",
    "zero_native_bypasses",
    "zero_state_writes",
    "zero_audit_activity",
)

LLM_EXECUTABLES = frozenset(
    {"anthropic", "claude", "codex", "gemini", "ollama", "openai"}
)
NETWORK_EXECUTABLES = frozenset(
    {"curl", "ftp", "nc", "scp", "sftp", "ssh", "telnet", "wget"}
)
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")


class InstrumentationViolation(OSError):
    """Raised when measured code attempts an out-of-contract operation."""


def _counter_template() -> dict[str, int]:
    return {
        "audit_reads": 0,
        "audit_writes": 0,
        "state_reads": 0,
        "state_writes": 0,
        "source_read_calls": 0,
        "source_bytes_read": 0,
        "provider_process_calls": 0,
        "git_process_calls": 0,
        "network_calls": 0,
        "llm_calls": 0,
        "native_bypass_attempts": 0,
    }


_ACTIVE_GUARDS: list[dict[str, int]] = []
_AUDIT_INSTALLED = False


def _process_argv(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, os.PathLike)):
        return (os.fsdecode(value),)
    if isinstance(value, Sequence):
        try:
            return tuple(os.fsdecode(item) for item in value)
        except (TypeError, ValueError):
            return ()
    return ()


def _record_process(counters: dict[str, int], argv: object, *, native: bool) -> None:
    counters["provider_process_calls"] += 1
    command = _process_argv(argv)
    executable = Path(command[0]).name.lower() if command else ""
    if executable == "git":
        counters["git_process_calls"] += 1
    if executable in LLM_EXECUTABLES:
        counters["llm_calls"] += 1
    if executable in NETWORK_EXECUTABLES:
        counters["network_calls"] += 1
    if native:
        counters["native_bypass_attempts"] += 1


def _audit_hook(event: str, arguments: tuple[object, ...]) -> None:
    if not _ACTIVE_GUARDS:
        return
    counters = _ACTIVE_GUARDS[-1]
    if event == "open" or event.startswith("mmap."):
        counters["source_read_calls"] += 1
        counters["native_bypass_attempts"] += 1
        raise InstrumentationViolation("native file I/O blocked by benchmark")
    if event == "subprocess.Popen":
        argv = arguments[1] if len(arguments) > 1 else arguments[0] if arguments else ()
        _record_process(counters, argv, native=True)
        raise InstrumentationViolation("native process blocked by benchmark")
    if event == "os.system" or event.startswith(
        ("os.exec", "os.fork", "os.posix_spawn", "os.spawn")
    ):
        argv = arguments[0] if arguments else ()
        _record_process(counters, argv, native=True)
        raise InstrumentationViolation("native process blocked by benchmark")
    if event.startswith("socket.") or event == "urllib.Request":
        counters["network_calls"] += 1
        counters["native_bypass_attempts"] += 1
        raise InstrumentationViolation("native network blocked by benchmark")


def _install_audit_hook() -> None:
    global _AUDIT_INSTALLED
    if not _AUDIT_INSTALLED:
        sys.addaudithook(_audit_hook)
        _AUDIT_INSTALLED = True


def _present_functions(module: object, names: Sequence[str]) -> tuple[object, ...]:
    return tuple(
        function
        for name in names
        if (function := getattr(module, name, None)) is not None
    )


_FILE_C_CALLS = _present_functions(
    os,
    (
        "open", "read", "readv", "pread", "preadv", "fdopen",
        "write", "writev", "pwrite", "pwritev",
    ),
)
_METADATA_C_CALLS = _present_functions(
    os,
    (
        "access", "fpathconf", "fstat", "fstatvfs", "getxattr",
        "listdir", "listxattr", "lstat", "pathconf", "readlink",
        "scandir", "stat", "statvfs",
    ),
)
_PROCESS_C_CALLS = _present_functions(
    os,
    (
        "fork", "forkpty", "posix_spawn", "posix_spawnp", "system",
        "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv",
        "spawnve", "spawnvp", "spawnvpe",
    ),
)
_NETWORK_C_CALLS = _present_functions(
    _socket,
    (
        "getaddrinfo", "gethostbyaddr", "gethostbyname",
        "gethostbyname_ex", "getnameinfo", "socket", "socketpair",
    ),
)
_NATIVE_SOCKET_TYPE = _socket.socket
_BOUND_FILE_IO_METHODS = frozenset(
    {
        "fileno", "flush", "read", "read1", "readall", "readinto",
        "readinto1", "readline", "readlines", "seek", "truncate", "write",
        "writelines",
    }
)
_BOUND_SOCKET_IO_METHODS = frozenset(
    {
        "accept", "bind", "connect", "connect_ex", "listen", "makefile",
        "recv", "recv_into", "recvfrom", "recvfrom_into", "recvmsg",
        "recvmsg_into", "send", "sendall", "sendmsg", "sendto",
        "sendfile", "shutdown",
    }
)


def _bound_instance_call(argument: object) -> str | None:
    owner = getattr(argument, "__self__", None)
    name = getattr(argument, "__name__", None)
    if isinstance(owner, io.IOBase) and name in _BOUND_FILE_IO_METHODS:
        return "file"
    if isinstance(owner, _NATIVE_SOCKET_TYPE) and name in _BOUND_SOCKET_IO_METHODS:
        return "network"
    return None


def _c_profile(
    previous: object,
    counters: dict[str, int],
    frame: object,
    event: str,
    argument: object,
) -> None:
    if previous is not None:
        previous(frame, event, argument)  # type: ignore[operator]
    if event != "c_call":
        return
    bound_kind = _bound_instance_call(argument)
    if bound_kind == "file":
        counters["source_read_calls"] += 1
        counters["native_bypass_attempts"] += 1
        raise InstrumentationViolation("prebound file instance I/O blocked")
    if bound_kind == "network":
        counters["network_calls"] += 1
        counters["native_bypass_attempts"] += 1
        raise InstrumentationViolation("prebound socket instance I/O blocked")
    if any(argument is function for function in (*_FILE_C_CALLS, *_METADATA_C_CALLS)):
        counters["source_read_calls"] += 1
        counters["native_bypass_attempts"] += 1
        raise InstrumentationViolation("prebound native file I/O blocked")
    if any(argument is function for function in _PROCESS_C_CALLS):
        _record_process(counters, (), native=True)
        raise InstrumentationViolation("prebound native process blocked")
    if any(argument is function for function in _NETWORK_C_CALLS):
        counters["network_calls"] += 1
        counters["native_bypass_attempts"] += 1
        raise InstrumentationViolation("prebound native network blocked")


@contextlib.contextmanager
def _guards() -> Iterator[dict[str, int]]:
    """Block current lookups and aliases bound before guard activation."""
    _install_audit_hook()
    counters = _counter_template()
    previous_profile = sys.getprofile()
    get_thread_profile = getattr(
        threading, "getprofile", lambda: getattr(threading, "_profile_hook", None)
    )
    previous_thread_profile = get_thread_profile()

    saved: list[tuple[object, str, object]] = []

    def replace(module: object, name: str, value: object) -> None:
        if hasattr(module, name):
            saved.append((module, name, getattr(module, name)))
            setattr(module, name, value)

    def blocked_file(*_args: object, **_kwargs: object) -> object:
        counters["source_read_calls"] += 1
        raise InstrumentationViolation("file I/O blocked by benchmark")

    def blocked_native_file(*_args: object, **_kwargs: object) -> object:
        counters["source_read_calls"] += 1
        counters["native_bypass_attempts"] += 1
        raise InstrumentationViolation("native file I/O blocked by benchmark")

    def blocked_process(argv: object = (), *_args: object, **_kwargs: object) -> object:
        _record_process(counters, argv, native=False)
        raise InstrumentationViolation("process blocked by benchmark")

    def blocked_network(*_args: object, **_kwargs: object) -> object:
        counters["network_calls"] += 1
        raise InstrumentationViolation("network blocked by benchmark")

    for module, names, function in (
        (builtins, ("open",), blocked_file),
        (io, ("open",), blocked_file),
        (_io, ("open",), blocked_native_file),
        (io, ("FileIO",), blocked_native_file),
        (_io, ("FileIO",), blocked_native_file),
        (os, ("open", "read", "readv", "pread", "preadv", "fdopen"), blocked_native_file),
        (os, ("write", "writev", "pwrite", "pwritev"), blocked_native_file),
        (os, ("access", "fpathconf", "fstat", "fstatvfs", "getxattr", "listdir", "listxattr", "lstat", "pathconf", "readlink", "scandir", "stat", "statvfs"), blocked_file),
        (mmap_module, ("mmap",), blocked_native_file),
        (subprocess, ("Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"), blocked_process),
        (os, ("system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"), blocked_process),
        (socket, ("socket", "SocketType", "create_connection", "socketpair", "getaddrinfo", "gethostbyaddr", "gethostbyname", "gethostbyname_ex", "getnameinfo"), blocked_network),
    ):
        for name in names:
            replace(module, name, function)

    def profile(frame: object, event: str, argument: object) -> None:
        _c_profile(previous_profile, counters, frame, event, argument)

    def thread_profile(frame: object, event: str, argument: object) -> None:
        _c_profile(previous_thread_profile, counters, frame, event, argument)

    _ACTIVE_GUARDS.append(counters)
    sys.setprofile(profile)
    threading.setprofile(thread_profile)
    try:
        yield counters
    finally:
        sys.setprofile(previous_profile)
        threading.setprofile(previous_thread_profile)
        if not _ACTIVE_GUARDS or _ACTIVE_GUARDS[-1] is not counters:
            raise RuntimeError("benchmark guard stack corrupted")
        _ACTIVE_GUARDS.pop()
        for module, name, value in reversed(saved):
            setattr(module, name, value)


@dataclass(frozen=True)
class _Corpus:
    snapshot: RepositorySnapshot
    host_inventory: HostInventory
    user_registry: tuple[ProviderDescriptor, ...]
    registration: ProjectRegistration | None
    consent: AuthorizationLedger
    request: BrokerRequest


class _CountingLedger(AuthorizationLedger):
    def __init__(
        self, records: tuple[object, ...], counters: dict[str, int]
    ) -> None:
        super().__init__(records)  # type: ignore[arg-type]
        object.__setattr__(self, "_benchmark_counters", counters)

    def decision_for(
        self,
        action: ContextAction,
        repository_identity: str,
        provider_identity: str,
        provider_schema_version: str,
    ) -> ConsentDisposition | None:
        self._benchmark_counters["state_reads"] += 1
        return super().decision_for(
            action,
            repository_identity,
            provider_identity,
            provider_schema_version,
        )


def _snapshot(markers: tuple[str, ...]) -> RepositorySnapshot:
    return RepositorySnapshot(
        "1",
        "sha256:benchmark-repository",
        "benchmark-root",
        "sha256:benchmark-root",
        "benchmark-git-dir",
        "benchmark-git-common-dir",
        "sha256:benchmark-git-common-dir",
        "sha256:benchmark-worktree",
        "a" * 40,
        "main",
        "sha256:benchmark-clean",
        True,
        ("README.md",),
        (),
        (),
        (),
        0,
        0,
        0,
        0,
        (("Python", 1),),
        (),
        tuple(sorted(markers)),
        0,
        0,
        0,
        (),
    )


def _capabilities(count: int) -> tuple[str, ...]:
    required = {"semantic-search", "status"}
    index = 0
    while len(required) < count:
        required.add("capability-{:02d}".format(index))
        index += 1
    return tuple(sorted(required))


def _descriptor(
    index: int,
    capabilities: tuple[str, ...],
    *,
    source: DiscoverySource,
    scenario: str,
) -> ProviderDescriptor:
    mixed = scenario in {
        "mixed-consent-freshness",
        "conflicts-denials-network-markers",
    }
    locality = (
        ProviderLocality.NETWORK_BACKED
        if mixed and index % 4 == 0
        else ProviderLocality.LOCAL
    )
    if mixed and index % 7 == 0:
        freshness = Freshness.STRUCTURALLY_STALE
    elif mixed and index % 5 == 0:
        freshness = Freshness.PARTIAL
    else:
        freshness = Freshness.EXACT
    marker_hints = (
        (".taf/markers/z-provider-{:03d}.marker".format(index),)
        if scenario == "conflicts-denials-network-markers" and index % 6 == 0
        else ()
    )
    return ProviderDescriptor(
        schema_version="1",
        provider_identity="z-provider-{:03d}".format(index),
        provider_version="1.0.0",
        provider_schema_version="1",
        capabilities=capabilities,
        locality=locality,
        discovery_sources=(source,),
        availability=Availability.AVAILABLE,
        registration=(
            Registration.UNREGISTERED
            if source is DiscoverySource.HOST_INVENTORY
            else Registration.USER_REGISTERED
        ),
        status_evidence=StatusEvidence.MANIFEST_VALIDATED,
        freshness=freshness,
        path_coverage=1.0,
        language_coverage=1.0,
        latency_ms=float(1 + index % 20),
        confidence=(
            Confidence.INFERRED if mixed and index % 11 == 0 else Confidence.VERIFIED
        ),
        supported_actions=(
            ContextAction.INSPECT,
            ContextAction.NETWORK,
            ContextAction.QUERY,
        ),
        required_actions=(),
        marker_hints=marker_hints,
        reason_codes=(),
        warnings=(),
    )


def _record(
    ledger: AuthorizationLedger,
    provider: ProviderDescriptor,
    disposition: ConsentDisposition,
    actions: tuple[ContextAction, ...],
) -> AuthorizationLedger:
    ordered = tuple(sorted(set(actions), key=lambda item: item.value))
    request = ConsentRequest.create(
        schema_version="1",
        repository_identity="sha256:benchmark-repository",
        provider_identity=provider.provider_identity,
        provider_schema_version=provider.provider_schema_version,
        actions=ordered,
        locality=provider.locality,
        data_surface=(
            "repository-metadata"
            if provider.locality is ProviderLocality.LOCAL
            else "repository-metadata-network"
        ),
        fallback="native-level-0",
        requested_at=UTC_NOW,
    )
    return ledger.record(request, disposition, UTC_NOW)


def _expected_counts(case: Mapping[str, object]) -> dict[str, int]:
    provider_count = int(case["provider_count"])
    capability_count = int(case["capabilities_per_provider"])
    discovered = min(provider_count, 64)
    retained_external = max(0, discovered - 1)
    return {
        "source_descriptor_count": provider_count - 1,
        "discovered_descriptor_count": discovered,
        "discovered_capability_count": (
            2 if provider_count == 1 else 2 + retained_external * capability_count
        ),
        "rejected_provider_count": int(
            case["expected_rejected_provider_count"]
        ),
        "omitted_provider_count": max(0, provider_count - 64),
    }


def _build_corpus(case: Mapping[str, object]) -> _Corpus:
    provider_count = int(case["provider_count"])
    capability_count = int(case["capabilities_per_provider"])
    scenario = str(case["scenario"])
    if provider_count == 1:
        return _Corpus(
            snapshot=_snapshot(()),
            host_inventory=HostInventory("1", (), 0, (), 0, False),
            user_registry=(),
            registration=None,
            consent=AuthorizationLedger(),
            request=BrokerRequest(
                "1",
                "benchmark-consumer",
                "sha256:benchmark-repository",
                "sha256:benchmark-worktree",
                "repository-map",
                Freshness.EXACT,
                1.0,
                None,
                False,
                None,
                MAX_MACHINE_DECISION_BYTES,
                MAX_MODEL_SUMMARY_CHARACTERS,
                None,
            ),
        )

    capabilities = _capabilities(capability_count)
    host: list[ProviderDescriptor] = []
    registry: list[ProviderDescriptor] = []
    registrations: list[ProjectRegistrationEntry] = []
    markers: set[str] = set()
    consent = AuthorizationLedger()
    providers: list[ProviderDescriptor] = []
    for index in range(provider_count - 1):
        if scenario == "conflicts-denials-network-markers":
            source = DiscoverySource.HOST_INVENTORY
        elif scenario == "mixed-consent-freshness" and index % 2 == 0:
            source = DiscoverySource.HOST_INVENTORY
        else:
            source = DiscoverySource.USER_REGISTRY
        provider = _descriptor(index, capabilities, source=source, scenario=scenario)
        providers.append(provider)
        markers.update(provider.marker_hints)
        if source is DiscoverySource.HOST_INVENTORY:
            host.append(provider)
            registrations.append(
                ProjectRegistrationEntry(
                    provider_identity=provider.provider_identity,
                    provider_schema_version=(
                        "9" if scenario == "conflicts-denials-network-markers" and index < 2 else "1"
                    ),
                    required_capabilities=("semantic-search",),
                )
            )
        else:
            registry.append(provider)

    for index, provider in enumerate(providers):
        if scenario in {"mixed-consent-freshness", "conflicts-denials-network-markers"}:
            if index % 7 == 0:
                consent = _record(
                    consent, provider, ConsentDisposition.DENY, (ContextAction.QUERY,)
                )
                continue
            if index % 5 == 0:
                continue
            consent = _record(
                consent, provider, ConsentDisposition.ALLOW, (ContextAction.QUERY,)
            )
            if provider.locality is ProviderLocality.NETWORK_BACKED:
                disposition = (
                    ConsentDisposition.DENY
                    if index % 9 == 0
                    else ConsentDisposition.ALLOW
                )
                consent = _record(
                    consent, provider, disposition, (ContextAction.NETWORK,)
                )
        else:
            consent = _record(
                consent, provider, ConsentDisposition.ALLOW, (ContextAction.QUERY,)
            )

    registration = (
        ProjectRegistration(
            "1",
            "sha256:benchmark-repository",
            tuple(sorted(registrations, key=lambda item: item.provider_identity)),
        )
        if registrations
        else None
    )
    return _Corpus(
        snapshot=_snapshot(tuple(markers)),
        host_inventory=HostInventory(
            "1", tuple(sorted(host, key=lambda item: item.provider_identity)), 0, (), 0, False
        ),
        user_registry=tuple(sorted(registry, key=lambda item: item.provider_identity)),
        registration=registration,
        consent=consent,
        request=BrokerRequest(
            "1",
            "benchmark-consumer",
            "sha256:benchmark-repository",
            "sha256:benchmark-worktree",
            "semantic-search",
            Freshness.PARTIAL,
            0.5,
            0.5,
            True,
            100.0,
            MAX_MACHINE_DECISION_BYTES,
            MAX_MODEL_SUMMARY_CHARACTERS,
            None,
        ),
    )


def _variant(corpus: _Corpus, mode: str, run_index: int) -> _Corpus:
    if mode == "canonical":
        return corpus

    def ordered(items: tuple[object, ...], salt: int) -> tuple[object, ...]:
        values = list(items)
        if mode == "reversed":
            values.reverse()
        elif mode == "permuted":
            random.Random(SEED + run_index * 101 + salt).shuffle(values)
        else:
            raise ValueError("variant-invalid")
        return tuple(values)

    registration = corpus.registration
    if registration is not None:
        registration = ProjectRegistration(
            registration.schema_version,
            registration.repository_identity,
            ordered(registration.providers, 3),  # type: ignore[arg-type]
        )
    return _Corpus(
        snapshot=corpus.snapshot,
        host_inventory=HostInventory(
            corpus.host_inventory.schema_version,
            ordered(corpus.host_inventory.providers, 1),  # type: ignore[arg-type]
            corpus.host_inventory.rejected_provider_count,
            corpus.host_inventory.rejection_summaries,
            corpus.host_inventory.omitted_provider_count,
            corpus.host_inventory.partial,
        ),
        user_registry=ordered(corpus.user_registry, 2),  # type: ignore[arg-type]
        registration=registration,
        consent=AuthorizationLedger(ordered(corpus.consent.records, 4)),  # type: ignore[arg-type]
        request=corpus.request,
    )


def _wire_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _input_metrics(corpus: _Corpus) -> dict[str, int]:
    registration_wire = (
        None if corpus.registration is None else corpus.registration.to_dict()
    )
    return {
        "host_inventory_bytes": len(_wire_bytes(corpus.host_inventory.to_dict())),
        "user_registry_bytes": len(
            _wire_bytes([item.to_dict() for item in corpus.user_registry])
        ),
        "registration_bytes": len(_wire_bytes(registration_wire)),
        "consent_bytes": len(_wire_bytes(corpus.consent.to_dict())),
        "request_bytes": len(_wire_bytes(corpus.request.to_dict())),
    }


def _execute(
    corpus: _Corpus,
    counters: dict[str, int],
) -> tuple[DiscoverySnapshot, RoutingDecision, bytes, bytes]:
    ledger = _CountingLedger(corpus.consent.records, counters)
    discovery = discover_providers(
        corpus.snapshot,
        corpus.host_inventory,
        corpus.user_registry,
        corpus.registration,
    )
    decision = route_provider(
        discovery,
        corpus.request,
        ledger,
        utc_now=UTC_NOW,
    )
    return (
        discovery,
        decision,
        _wire_bytes(discovery.to_dict()),
        _wire_bytes(decision.to_dict()),
    )


_INTEGER_METRICS = (
    "peak_rss_bytes",
    "host_inventory_bytes",
    "user_registry_bytes",
    "registration_bytes",
    "consent_bytes",
    "request_bytes",
    "discovery_input_bytes",
    "discovery_output_bytes",
    "routing_input_bytes",
    "routing_output_bytes",
    "model_summary_characters",
    "discovery_artifact_bytes",
    "corpus_provider_count",
    "external_provider_count",
    "capabilities_per_external_provider",
    "source_descriptor_count",
    "discovered_descriptor_count",
    "discovered_capability_count",
    "rejected_provider_count",
    "omitted_provider_count",
    "audit_reads",
    "audit_writes",
    "state_reads",
    "state_writes",
    "source_read_calls",
    "source_bytes_read",
    "provider_process_calls",
    "git_process_calls",
    "network_calls",
    "llm_calls",
    "native_bypass_attempts",
)
_NUMBER_METRICS = (
    "cold_wall_seconds",
    "cold_cpu_seconds",
    "warm_wall_seconds",
    "warm_cpu_seconds",
    "discovery_warm_seconds",
    "routing_warm_seconds",
    "consent_decision_warm_seconds",
)
_HASH_FIELDS = (
    "discovery_output_sha256",
    "routing_output_sha256",
    "reversed_discovery_output_sha256",
    "reversed_routing_output_sha256",
    "permuted_discovery_output_sha256",
    "permuted_routing_output_sha256",
)
SAMPLE_FIELDS = frozenset(
    {
        "status",
        "case_name",
        "run_index",
        "correctness_passed",
        "correctness_failure",
        "performance_failure",
        "correctness_checks",
        "worker_exit_code",
        *_INTEGER_METRICS,
        *_NUMBER_METRICS,
        *_HASH_FIELDS,
    }
)


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value) if sys.platform == "darwin" else int(value) * 1024


def _worker(case: Mapping[str, object], run_index: int) -> int:
    canonical = _build_corpus(case)
    reversed_corpus = _variant(canonical, "reversed", run_index)
    permuted_corpus = _variant(canonical, "permuted", run_index)
    inputs = _input_metrics(canonical)
    expected = _expected_counts(case)

    with _guards() as counters:
        warm_cpu_started = time.process_time()
        warm_started = time.perf_counter()

        ledger = _CountingLedger(canonical.consent.records, counters)
        discovery_started = time.perf_counter()
        discovery = discover_providers(
            canonical.snapshot,
            canonical.host_inventory,
            canonical.user_registry,
            canonical.registration,
        )
        discovery_seconds = time.perf_counter() - discovery_started
        discovery_bytes = _wire_bytes(discovery.to_dict())

        routing_started = time.perf_counter()
        decision = route_provider(
            discovery,
            canonical.request,
            ledger,
            utc_now=UTC_NOW,
        )
        routing_seconds = time.perf_counter() - routing_started
        decision_bytes = _wire_bytes(decision.to_dict())

        consent_started = time.perf_counter()
        repeated_decision = route_provider(
            discovery,
            canonical.request,
            ledger,
            utc_now=UTC_NOW,
        )
        consent_seconds = time.perf_counter() - consent_started
        repeated_decision_bytes = _wire_bytes(repeated_decision.to_dict())

        reversed_discovery, reversed_decision, reversed_discovery_bytes, reversed_decision_bytes = _execute(
            reversed_corpus, counters
        )
        permuted_discovery, permuted_decision, permuted_discovery_bytes, permuted_decision_bytes = _execute(
            permuted_corpus, counters
        )

        discovery_round_trip = (
            DiscoverySnapshot.from_dict(discovery.to_dict()) == discovery
            and DiscoverySnapshot.from_dict(reversed_discovery.to_dict())
            == reversed_discovery
            and DiscoverySnapshot.from_dict(permuted_discovery.to_dict())
            == permuted_discovery
        )
        routing_round_trip = (
            RoutingDecision.from_dict(decision.to_dict()) == decision
            and RoutingDecision.from_dict(reversed_decision.to_dict())
            == reversed_decision
            and RoutingDecision.from_dict(permuted_decision.to_dict())
            == permuted_decision
        )

        warm_wall_seconds = time.perf_counter() - warm_started
        warm_cpu_seconds = time.process_time() - warm_cpu_started

    discovered_capabilities = sum(
        len(provider.capabilities) for provider in discovery.providers
    )
    routing_input_bytes = len(
        _wire_bytes(
            {
                "consent": canonical.consent.to_dict(),
                "discovery": discovery.to_dict(),
                "request": canonical.request.to_dict(),
            }
        )
    )
    source_descriptor_count = len(canonical.host_inventory.providers) + len(
        canonical.user_registry
    )
    hashes = {
        "discovery_output_sha256": _sha256(discovery_bytes),
        "routing_output_sha256": _sha256(decision_bytes),
        "reversed_discovery_output_sha256": _sha256(reversed_discovery_bytes),
        "reversed_routing_output_sha256": _sha256(reversed_decision_bytes),
        "permuted_discovery_output_sha256": _sha256(permuted_discovery_bytes),
        "permuted_routing_output_sha256": _sha256(permuted_decision_bytes),
    }
    checks = {
        "corpus_provider_count_exact": int(case["provider_count"])
        == 1 + source_descriptor_count,
        "source_descriptor_count_exact": source_descriptor_count
        == expected["source_descriptor_count"],
        "capabilities_per_external_provider_exact": all(
            len(provider.capabilities) == int(case["capabilities_per_provider"])
            for provider in (
                *canonical.host_inventory.providers,
                *canonical.user_registry,
            )
        ),
        "discovered_descriptor_count_exact": len(discovery.providers)
        == expected["discovered_descriptor_count"],
        "discovered_capability_count_exact": discovered_capabilities
        == expected["discovered_capability_count"],
        "rejected_provider_count_exact": discovery.rejected_provider_count
        == expected["rejected_provider_count"],
        "omitted_provider_count_exact": discovery.omitted_provider_count
        == expected["omitted_provider_count"],
        "host_inventory_within_256_kib": inputs["host_inventory_bytes"]
        <= MAX_HOST_INVENTORY_BYTES,
        "model_summary_within_2000_characters": len(decision.model_summary)
        <= MAX_MODEL_SUMMARY_CHARACTERS,
        "machine_decision_within_16_kib": len(decision_bytes)
        <= MAX_MACHINE_DECISION_BYTES,
        "discovery_artifact_within_256_kib": len(discovery_bytes)
        <= MAX_DISCOVERY_ARTIFACT_BYTES,
        "discovery_wire_round_trip": discovery_round_trip,
        "routing_wire_round_trip": routing_round_trip,
        "repeat_routing_byte_identical": repeated_decision_bytes == decision_bytes,
        "reversed_discovery_byte_identical": reversed_discovery_bytes
        == discovery_bytes,
        "reversed_routing_byte_identical": reversed_decision_bytes
        == decision_bytes,
        "permuted_discovery_byte_identical": permuted_discovery_bytes
        == discovery_bytes,
        "permuted_routing_byte_identical": permuted_decision_bytes
        == decision_bytes,
        "zero_source_reads": counters["source_read_calls"] == 0
        and counters["source_bytes_read"] == 0,
        "zero_provider_processes": counters["provider_process_calls"] == 0,
        "zero_git_processes": counters["git_process_calls"] == 0,
        "zero_network_calls": counters["network_calls"] == 0,
        "zero_llm_calls": counters["llm_calls"] == 0,
        "zero_native_bypasses": counters["native_bypass_attempts"] == 0,
        "zero_state_writes": counters["state_writes"] == 0,
        "zero_audit_activity": counters["audit_reads"] == 0
        and counters["audit_writes"] == 0,
    }
    passed = all(checks.values())
    sample = {
        "status": "ok" if passed else "correctness-failure",
        "case_name": case["name"],
        "run_index": run_index,
        "correctness_passed": passed,
        "correctness_failure": not passed,
        "performance_failure": False,
        "cold_wall_seconds": 0.0,
        "cold_cpu_seconds": 0.0,
        "warm_wall_seconds": warm_wall_seconds,
        "warm_cpu_seconds": warm_cpu_seconds,
        "discovery_warm_seconds": discovery_seconds,
        "routing_warm_seconds": routing_seconds,
        "consent_decision_warm_seconds": consent_seconds,
        "peak_rss_bytes": _rss_bytes(),
        **inputs,
        "discovery_input_bytes": discovery.input_bytes,
        "discovery_output_bytes": len(discovery_bytes),
        "routing_input_bytes": routing_input_bytes,
        "routing_output_bytes": len(decision_bytes),
        "model_summary_characters": len(decision.model_summary),
        "discovery_artifact_bytes": len(discovery_bytes),
        "corpus_provider_count": int(case["provider_count"]),
        "external_provider_count": int(case["provider_count"]) - 1,
        "capabilities_per_external_provider": int(
            case["capabilities_per_provider"]
        ),
        "source_descriptor_count": source_descriptor_count,
        "discovered_descriptor_count": len(discovery.providers),
        "discovered_capability_count": discovered_capabilities,
        "rejected_provider_count": discovery.rejected_provider_count,
        "omitted_provider_count": discovery.omitted_provider_count,
        **counters,
        **hashes,
        "correctness_checks": checks,
        "worker_exit_code": 0 if passed else 1,
    }
    sys.stdout.write(
        json.dumps(
            sample,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0 if passed else 1


TIMEOUT_SAMPLE_FIELDS = frozenset(
    {
        "status",
        "error",
        "case_name",
        "run_index",
        "correctness_passed",
        "correctness_failure",
        "performance_failure",
        "cold_wall_seconds",
        "cold_cpu_seconds",
        "worker_exit_code",
    }
)
ERROR_SAMPLE_FIELDS = frozenset(
    {
        "status",
        "error",
        "case_name",
        "run_index",
        "correctness_passed",
        "correctness_failure",
        "performance_failure",
        "cold_wall_seconds",
        "cold_cpu_seconds",
        "worker_exit_code",
        "worker_stdout_bytes",
        "worker_stderr_bytes",
        "worker_stdout_excerpt",
        "worker_stderr_excerpt",
    }
)
STRUCTURE_SAMPLE_FIELDS = frozenset(
    {*ERROR_SAMPLE_FIELDS, "structure_errors", "worker_result_excerpt"}
)


class _DuplicateKeyError(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def _excerpt(value: str) -> str:
    raw = value.encode("utf-8", "replace")[:MAX_DIAGNOSTIC_BYTES]
    return raw.decode("utf-8", "replace")


def _child_cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return float(usage.ru_utime + usage.ru_stime)


def _is_nonnegative_integer(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_INTEGER


def _is_finite_nonnegative_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(converted) and converted >= 0.0


def _is_sane_timing(value: object) -> bool:
    return (
        _is_finite_nonnegative_number(value)
        and float(value) <= MAX_TIMING_SECONDS
    )


def _sample_structure_errors(
    sample: dict[str, object],
    case: Mapping[str, object],
    run_index: int,
) -> list[str]:
    errors: list[str] = []
    if set(sample) != SAMPLE_FIELDS:
        errors.append("sample-fields")
    if sample.get("status") not in {"ok", "correctness-failure"}:
        errors.append("status")
    if sample.get("case_name") != case["name"]:
        errors.append("case_name")
    if (
        type(sample.get("run_index")) is not int
        or sample.get("run_index") != run_index
    ):
        errors.append("run_index")
    if type(sample.get("correctness_passed")) is not bool:
        errors.append("correctness_passed")
    if type(sample.get("correctness_failure")) is not bool:
        errors.append("correctness_failure")
    if type(sample.get("performance_failure")) is not bool:
        errors.append("performance_failure")
    if sample.get("performance_failure") is not False:
        errors.append("performance_failure")
    for field in _INTEGER_METRICS:
        if not _is_nonnegative_integer(sample.get(field)):
            errors.append(field)
    for field in _NUMBER_METRICS:
        if not _is_sane_timing(sample.get(field)):
            errors.append(field)
    for field in _HASH_FIELDS:
        value = sample.get(field)
        if not isinstance(value, str) or not _HEX_64.fullmatch(value):
            errors.append(field)

    checks = sample.get("correctness_checks")
    if (
        type(checks) is not dict
        or set(checks) != set(CHECK_NAMES)
        or any(type(value) is not bool for value in checks.values())
    ):
        errors.append("correctness_checks")
    elif sample.get("status") == "ok" and not all(checks.values()):
        errors.append("correctness_checks")

    expected = _expected_counts(case)
    exact = {
        "corpus_provider_count": int(case["provider_count"]),
        "external_provider_count": int(case["provider_count"]) - 1,
        "capabilities_per_external_provider": int(
            case["capabilities_per_provider"]
        ),
        **expected,
    }
    for field, value in exact.items():
        if sample.get(field) != value:
            errors.append(field)

    if sample.get("source_descriptor_count") != int(case["provider_count"]) - 1:
        errors.append("source_descriptor_count")
    if sample.get("host_inventory_bytes", MAX_HOST_INVENTORY_BYTES + 1) > MAX_HOST_INVENTORY_BYTES:
        errors.append("host_inventory_bytes")
    if sample.get("model_summary_characters", MAX_MODEL_SUMMARY_CHARACTERS + 1) > MAX_MODEL_SUMMARY_CHARACTERS:
        errors.append("model_summary_characters")
    if sample.get("routing_output_bytes", MAX_MACHINE_DECISION_BYTES + 1) > MAX_MACHINE_DECISION_BYTES:
        errors.append("routing_output_bytes")
    if sample.get("discovery_artifact_bytes", MAX_DISCOVERY_ARTIFACT_BYTES + 1) > MAX_DISCOVERY_ARTIFACT_BYTES:
        errors.append("discovery_artifact_bytes")

    for field in (
        "source_read_calls",
        "source_bytes_read",
        "provider_process_calls",
        "git_process_calls",
        "network_calls",
        "llm_calls",
        "native_bypass_attempts",
        "state_writes",
        "audit_reads",
        "audit_writes",
    ):
        if sample.get(field) != 0:
            errors.append(field)

    if sample.get("discovery_output_sha256") != sample.get(
        "reversed_discovery_output_sha256"
    ) or sample.get("discovery_output_sha256") != sample.get(
        "permuted_discovery_output_sha256"
    ):
        errors.append("discovery-permutations")
    if sample.get("routing_output_sha256") != sample.get(
        "reversed_routing_output_sha256"
    ) or sample.get("routing_output_sha256") != sample.get(
        "permuted_routing_output_sha256"
    ):
        errors.append("routing-permutations")

    worker_exit = sample.get("worker_exit_code")
    if type(worker_exit) is not int:
        errors.append("worker_exit_code")
    if sample.get("status") == "ok" and worker_exit != 0:
        errors.append("worker_exit_code")
    if sample.get("status") == "ok" and sample.get("correctness_passed") is not True:
        errors.append("correctness_passed")
    if sample.get("status") == "ok" and sample.get("correctness_failure") is not False:
        errors.append("correctness_failure")
    return sorted(set(errors))


def _protocol_error(
    case: Mapping[str, object],
    run_index: int,
    *,
    error: str,
    stdout: str,
    stderr: str,
    cold_wall_seconds: float,
    cold_cpu_seconds: float,
    worker_exit_code: int | None,
) -> dict[str, object]:
    return {
        "status": "worker-protocol-error",
        "error": error,
        "case_name": case["name"],
        "run_index": run_index,
        "correctness_passed": False,
        "correctness_failure": True,
        "performance_failure": False,
        "cold_wall_seconds": cold_wall_seconds,
        "cold_cpu_seconds": cold_cpu_seconds,
        "worker_exit_code": worker_exit_code,
        "worker_stdout_bytes": len(stdout.encode("utf-8", "replace")),
        "worker_stderr_bytes": len(stderr.encode("utf-8", "replace")),
        "worker_stdout_excerpt": _excerpt(stdout),
        "worker_stderr_excerpt": _excerpt(stderr),
    }


def _measured_worker(
    case: Mapping[str, object], run_index: int
) -> dict[str, object]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker-case",
        str(case["name"]),
        "--worker-run",
        str(run_index),
    ]
    cpu_before = _child_cpu_seconds()
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=WORKER_TIMEOUT_SECONDS,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "timeout",
            "error": "worker exceeded {} second timeout".format(exc.timeout),
            "case_name": case["name"],
            "run_index": run_index,
            "correctness_passed": None,
            "correctness_failure": False,
            "performance_failure": True,
            "cold_wall_seconds": time.perf_counter() - started,
            "cold_cpu_seconds": max(0.0, _child_cpu_seconds() - cpu_before),
            "worker_exit_code": None,
        }

    cold_wall = time.perf_counter() - started
    cold_cpu = max(0.0, _child_cpu_seconds() - cpu_before)
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    stderr = completed.stderr if isinstance(completed.stderr, str) else ""
    if (
        len(stdout.encode("utf-8", "replace")) > MAX_WORKER_OUTPUT_BYTES
        or len(stderr.encode("utf-8", "replace")) > MAX_WORKER_OUTPUT_BYTES
    ):
        return _protocol_error(
            case,
            run_index,
            error="worker output exceeded byte ceiling",
            stdout=stdout,
            stderr=stderr,
            cold_wall_seconds=cold_wall,
            cold_cpu_seconds=cold_cpu,
            worker_exit_code=completed.returncode,
        )
    try:
        parsed = json.loads(
            stdout,
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: float(value),
        )
        if type(parsed) is not dict or "status" not in parsed or "correctness_passed" not in parsed:
            raise ValueError("worker JSON is not a result object")
    except (TypeError, ValueError, json.JSONDecodeError, _DuplicateKeyError) as exc:
        return _protocol_error(
            case,
            run_index,
            error="{}: {}".format(type(exc).__name__, exc),
            stdout=stdout,
            stderr=stderr,
            cold_wall_seconds=cold_wall,
            cold_cpu_seconds=cold_cpu,
            worker_exit_code=completed.returncode,
        )

    sample = dict(parsed)
    sample["cold_wall_seconds"] = cold_wall
    sample["cold_cpu_seconds"] = cold_cpu
    sample["worker_exit_code"] = completed.returncode
    errors = _sample_structure_errors(sample, case, run_index)
    if errors:
        protocol = _protocol_error(
            case,
            run_index,
            error="missing or invalid mandatory metrics: {}".format(
                ", ".join(errors)
            ),
            stdout=stdout,
            stderr=stderr,
            cold_wall_seconds=cold_wall,
            cold_cpu_seconds=cold_cpu,
            worker_exit_code=completed.returncode,
        )
        protocol["status"] = "worker-structure-error"
        protocol["structure_errors"] = errors
        protocol["worker_result_excerpt"] = _excerpt(
            json.dumps(sample, ensure_ascii=False, sort_keys=True, default=str)
        )
        return protocol

    sample["correctness_failure"] = (
        sample.get("correctness_passed") is not True or completed.returncode != 0
    )
    if sample["correctness_failure"] and sample.get("status") == "ok":
        sample["status"] = "correctness-failure"
    return sample


GATE_NAMES = (
    "warmup_completed",
    "all_five_samples_retained",
    "all_measured_samples_completed",
    "routing_warm_p95_at_most_0_050_seconds",
    "consent_decision_warm_p95_at_most_0_010_seconds",
    "model_summary_within_2000_characters",
    "machine_decision_within_16_kib",
    "discovery_artifacts_within_256_kib",
    "host_inventory_within_256_kib",
    "zero_source_bytes",
    "zero_provider_processes",
    "zero_git_processes",
    "zero_network_calls",
    "zero_llm_calls",
    "zero_native_bypasses",
    "zero_state_and_audit_writes",
    "equivalent_permutations_byte_identical",
    "expected_descriptor_capability_and_overflow_counts",
)
_SUMMARY_FIELDS = frozenset({"p50", "p95", "sample_count", "samples"})
CLASS_FIELDS = frozenset(
    {
        "status",
        "name",
        "corpus",
        "error",
        "warmup",
        "samples",
        "wall_time",
        "cpu_time",
        "peak_rss_bytes",
        "input_bytes",
        "output_bytes",
        "counts",
        "forbidden_counter_totals",
        "corpus_checks",
        "gates",
        "performance_failures",
        "correctness_passed",
        "mandatory_gates_passed",
    }
)
EVIDENCE_FIELDS = frozenset(
    {
        "schema",
        "seed",
        "warm_up_runs_per_class",
        "measured_runs_per_class",
        "percentile_method",
        "timing_definitions",
        "thresholds",
        "machine",
        "classes",
        "correctness_passed",
        "mandatory_gates_passed",
        "python_retention_decision",
    }
)


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _summary(
    samples: Sequence[Mapping[str, object]], field: str
) -> dict[str, object]:
    values = [
        float(sample[field])
        for sample in samples
        if sample.get("status") == "ok"
        and _is_finite_nonnegative_number(sample.get(field))
    ]
    return {
        "p50": _percentile(values, 0.50) if values else None,
        "p95": _percentile(values, 0.95) if values else None,
        "sample_count": len(values),
        "samples": values,
    }


def _all_samples(
    samples: Sequence[Mapping[str, object]],
    field: str,
    predicate: object,
) -> bool:
    return len(samples) == MEASURED_RUNS and all(
        predicate(sample.get(field)) for sample in samples  # type: ignore[operator]
    )


def _permutations_identical(sample: Mapping[str, object]) -> bool:
    return (
        sample.get("discovery_output_sha256")
        == sample.get("reversed_discovery_output_sha256")
        == sample.get("permuted_discovery_output_sha256")
        and sample.get("routing_output_sha256")
        == sample.get("reversed_routing_output_sha256")
        == sample.get("permuted_routing_output_sha256")
    )


def _assemble_class(
    case: Mapping[str, object],
    warmup: Mapping[str, object],
    samples: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    retained = [dict(sample) for sample in samples]
    complete = len(retained) == MEASURED_RUNS and all(
        sample.get("status") == "ok" for sample in retained
    )
    expected = _expected_counts(case)
    count_fields = (
        "corpus_provider_count",
        "external_provider_count",
        "capabilities_per_external_provider",
        "source_descriptor_count",
        "discovered_descriptor_count",
        "discovered_capability_count",
        "rejected_provider_count",
        "omitted_provider_count",
    )
    expected_counts = {
        "corpus_provider_count": int(case["provider_count"]),
        "external_provider_count": int(case["provider_count"]) - 1,
        "capabilities_per_external_provider": int(
            case["capabilities_per_provider"]
        ),
        **expected,
    }
    counts_exact = complete and all(
        all(sample.get(field) == expected_counts[field] for field in count_fields)
        for sample in retained
    )
    permutations = complete and all(_permutations_identical(sample) for sample in retained)
    warmup_ok = warmup.get("status") == "ok" and warmup.get(
        "correctness_passed"
    ) is True
    samples_correct = complete and all(
        sample.get("correctness_passed") is True
        and sample.get("correctness_failure") is False
        for sample in retained
    )
    corpus_checks = {
        "fixture_preflight_completed": True,
        "warmup_has_no_correctness_failure": warmup_ok,
        "samples_have_no_correctness_failure": samples_correct,
        "descriptor_capability_and_overflow_counts_exact": counts_exact,
        "equivalent_permutations_byte_identical": permutations,
    }

    routing = _summary(retained, "routing_warm_seconds")
    consent = _summary(retained, "consent_decision_warm_seconds")
    routing_limit = case["routing_warm_p95_limit_seconds"]
    consent_limit = case["consent_decision_warm_p95_limit_seconds"]
    zero_fields = (
        "source_read_calls",
        "source_bytes_read",
        "provider_process_calls",
        "git_process_calls",
        "network_calls",
        "llm_calls",
        "native_bypass_attempts",
    )
    gates = {
        "warmup_completed": warmup_ok,
        "all_five_samples_retained": len(retained) == MEASURED_RUNS,
        "all_measured_samples_completed": complete,
        "routing_warm_p95_at_most_0_050_seconds": complete
        and (
            routing_limit is None
            or (
                routing["p95"] is not None
                and float(routing["p95"]) <= float(routing_limit)
            )
        ),
        "consent_decision_warm_p95_at_most_0_010_seconds": complete
        and (
            consent_limit is None
            or (
                consent["p95"] is not None
                and float(consent["p95"]) <= float(consent_limit)
            )
        ),
        "model_summary_within_2000_characters": _all_samples(
            retained,
            "model_summary_characters",
            lambda value: _is_nonnegative_integer(value)
            and value <= MAX_MODEL_SUMMARY_CHARACTERS,
        ),
        "machine_decision_within_16_kib": _all_samples(
            retained,
            "routing_output_bytes",
            lambda value: _is_nonnegative_integer(value)
            and value <= MAX_MACHINE_DECISION_BYTES,
        ),
        "discovery_artifacts_within_256_kib": _all_samples(
            retained,
            "discovery_artifact_bytes",
            lambda value: _is_nonnegative_integer(value)
            and value <= MAX_DISCOVERY_ARTIFACT_BYTES,
        ),
        "host_inventory_within_256_kib": _all_samples(
            retained,
            "host_inventory_bytes",
            lambda value: _is_nonnegative_integer(value)
            and value <= MAX_HOST_INVENTORY_BYTES,
        ),
        "zero_source_bytes": complete
        and all(
            sample.get("source_read_calls") == 0
            and sample.get("source_bytes_read") == 0
            for sample in retained
        ),
        "zero_provider_processes": complete
        and all(sample.get("provider_process_calls") == 0 for sample in retained),
        "zero_git_processes": complete
        and all(sample.get("git_process_calls") == 0 for sample in retained),
        "zero_network_calls": complete
        and all(sample.get("network_calls") == 0 for sample in retained),
        "zero_llm_calls": complete
        and all(sample.get("llm_calls") == 0 for sample in retained),
        "zero_native_bypasses": complete
        and all(sample.get("native_bypass_attempts") == 0 for sample in retained),
        "zero_state_and_audit_writes": complete
        and all(
            sample.get("state_writes") == 0
            and sample.get("audit_reads") == 0
            and sample.get("audit_writes") == 0
            for sample in retained
        ),
        "equivalent_permutations_byte_identical": permutations,
        "expected_descriptor_capability_and_overflow_counts": counts_exact,
    }
    forbidden_totals = {
        field: sum(
            int(sample.get(field, 0))
            for sample in retained
            if _is_nonnegative_integer(sample.get(field, 0))
        )
        for field in (*zero_fields, "audit_reads", "audit_writes", "state_reads", "state_writes")
    }
    result = {
        "status": "ok",
        "name": case["name"],
        "corpus": dict(case),
        "error": None,
        "warmup": dict(warmup),
        "samples": retained,
        "wall_time": {
            "cold_seconds": _summary(retained, "cold_wall_seconds"),
            "warm_seconds": _summary(retained, "warm_wall_seconds"),
            "discovery_seconds": _summary(retained, "discovery_warm_seconds"),
            "routing_seconds": routing,
            "consent_lookup_decision_seconds": consent,
        },
        "cpu_time": {
            "cold_seconds": _summary(retained, "cold_cpu_seconds"),
            "warm_seconds": _summary(retained, "warm_cpu_seconds"),
        },
        "peak_rss_bytes": _summary(retained, "peak_rss_bytes"),
        "input_bytes": {
            field: _summary(retained, field)
            for field in (
                "host_inventory_bytes",
                "user_registry_bytes",
                "registration_bytes",
                "consent_bytes",
                "request_bytes",
                "discovery_input_bytes",
                "routing_input_bytes",
            )
        },
        "output_bytes": {
            field: _summary(retained, field)
            for field in (
                "discovery_output_bytes",
                "routing_output_bytes",
                "discovery_artifact_bytes",
                "model_summary_characters",
            )
        },
        "counts": {
            field: _summary(retained, field) for field in count_fields
        },
        "forbidden_counter_totals": forbidden_totals,
        "corpus_checks": corpus_checks,
        "gates": gates,
        "performance_failures": [
            {
                "run": index + 1,
                "status": sample.get("status"),
                "error": sample.get("error"),
            }
            for index, sample in enumerate(retained)
            if sample.get("performance_failure") is True
        ],
        "correctness_passed": all(corpus_checks.values()),
        "mandatory_gates_passed": all(gates.values()),
    }
    return result


def _case_result(case: Mapping[str, object]) -> dict[str, object]:
    _build_corpus(case)  # Deterministic fixture preflight, outside all timers.
    warmup = _measured_worker(case, 0)
    samples = [_measured_worker(case, index) for index in range(1, 6)]
    return _assemble_class(case, warmup, samples)


def _failed_class(
    case: Mapping[str, object], error: Exception
) -> dict[str, object]:
    false_gates = {name: False for name in GATE_NAMES}
    return {
        "status": "class-error",
        "name": case["name"],
        "corpus": dict(case),
        "error": _excerpt("{}: {}".format(type(error).__name__, error)),
        "warmup": None,
        "samples": [],
        "wall_time": {},
        "cpu_time": {},
        "peak_rss_bytes": {},
        "input_bytes": {},
        "output_bytes": {},
        "counts": {},
        "forbidden_counter_totals": {},
        "corpus_checks": {
            "fixture_preflight_completed": False,
            "warmup_has_no_correctness_failure": False,
            "samples_have_no_correctness_failure": False,
            "descriptor_capability_and_overflow_counts_exact": False,
            "equivalent_permutations_byte_identical": False,
        },
        "gates": false_gates,
        "performance_failures": [],
        "correctness_passed": False,
        "mandatory_gates_passed": False,
    }


MACHINE_FIELDS = (
    "hostname",
    "machine",
    "processor",
    "platform",
    "python",
    "python_executable",
    "python_implementation",
    "cpu_count",
    "git",
)


def _machine_record(value: Mapping[str, object]) -> dict[str, object]:
    defaults: dict[str, object] = {
        "hostname": "unavailable",
        "machine": "unavailable",
        "processor": "unavailable",
        "platform": "unavailable",
        "python": "unavailable",
        "python_executable": "unavailable",
        "python_implementation": "unavailable",
        "cpu_count": None,
        "git": "not invoked by pure benchmark",
    }
    for field in MACHINE_FIELDS:
        if field in value:
            defaults[field] = value[field]
    return defaults


def _machine() -> dict[str, object]:
    return _machine_record(
        {
            "hostname": platform.node(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "platform": platform.platform(),
            "python": sys.version.replace("\n", " "),
            "python_executable": sys.executable,
            "python_implementation": platform.python_implementation(),
            "cpu_count": os.cpu_count(),
            "git": "not invoked by pure benchmark",
        }
    )


def _retention_decision(correctness_passed: bool, gates_passed: bool) -> str:
    if correctness_passed and gates_passed:
        return "GO — Retain Python for discovery and routing."
    return (
        "NO-GO — Keep the contract; write a replacement bakeoff plan before "
        "production implementation continues."
    )


def _evidence(
    classes: Sequence[Mapping[str, object]],
    *,
    machine: Mapping[str, object],
) -> dict[str, object]:
    results = [dict(item) for item in classes]
    correctness_passed = bool(results) and all(
        item.get("correctness_passed") is True for item in results
    )
    mandatory_gates_passed = bool(results) and all(
        item.get("mandatory_gates_passed") is True for item in results
    )
    return {
        "schema": SCHEMA,
        "seed": SEED,
        "warm_up_runs_per_class": WARM_UP_RUNS,
        "measured_runs_per_class": MEASURED_RUNS,
        "percentile_method": "nearest-rank",
        "timing_definitions": dict(TIMING_DEFINITIONS),
        "thresholds": dict(THRESHOLDS),
        "machine": _machine_record(machine),
        "classes": results,
        "correctness_passed": correctness_passed,
        "mandatory_gates_passed": mandatory_gates_passed,
        "python_retention_decision": _retention_decision(
            correctness_passed, mandatory_gates_passed
        ),
    }


def _validate_summary(value: object, field: str) -> None:
    if type(value) is not dict or set(value) != _SUMMARY_FIELDS:
        raise ValueError("{} summary fields".format(field))
    samples = value["samples"]
    upper_bound = (
        MAX_TIMING_SECONDS if "seconds" in field else MAX_INTEGER
    )
    if type(samples) is not list or any(
        not _is_finite_nonnegative_number(item)
        or float(item) > upper_bound
        for item in samples
    ):
        raise ValueError("{} summary samples".format(field))
    if (
        type(value["sample_count"]) is not int
        or value["sample_count"] != len(samples)
    ):
        raise ValueError("{} summary sample_count".format(field))
    for percentile in ("p50", "p95"):
        metric = value[percentile]
        if metric is not None and (
            not _is_finite_nonnegative_number(metric)
            or float(metric) > upper_bound
        ):
            raise ValueError("{} summary {}".format(field, percentile))


def _validate_sample_shape(
    value: object,
    case: Mapping[str, object],
    field: str,
    expected_run_index: int,
) -> None:
    if type(value) is not dict:
        raise ValueError("{} sample".format(field))
    status = value.get("status")
    expected_fields = (
        SAMPLE_FIELDS
        if status in {"ok", "correctness-failure"}
        else TIMEOUT_SAMPLE_FIELDS
        if status == "timeout"
        else STRUCTURE_SAMPLE_FIELDS
        if status == "worker-structure-error"
        else ERROR_SAMPLE_FIELDS
        if status == "worker-protocol-error"
        else frozenset()
    )
    if not expected_fields or set(value) != expected_fields:
        raise ValueError("{} sample fields".format(field))
    if status in {"ok", "correctness-failure"}:
        if (
            type(value.get("run_index")) is not int
            or value.get("run_index") != expected_run_index
        ):
            raise ValueError("{} run index".format(field))
        structural = _sample_structure_errors(value, case, expected_run_index)
        if structural:
            raise ValueError("{} sample structure: {}".format(field, structural))
    else:
        if (
            type(value.get("run_index")) is not int
            or value.get("run_index") != expected_run_index
        ):
            raise ValueError("{} run index".format(field))
        for metric in ("cold_wall_seconds", "cold_cpu_seconds"):
            if not _is_sane_timing(value.get(metric)):
                raise ValueError("{} {}".format(field, metric))
        if not isinstance(value.get("error"), str):
            raise ValueError("{} error".format(field))
        worker_exit_code = value.get("worker_exit_code")
        if status == "timeout":
            if worker_exit_code is not None:
                raise ValueError("{} worker exit code".format(field))
        elif (
            type(worker_exit_code) is not int
            or not -MAX_INTEGER <= worker_exit_code <= MAX_INTEGER
        ):
            raise ValueError("{} worker exit code".format(field))
        if status != "timeout":
            for counter in ("worker_stdout_bytes", "worker_stderr_bytes"):
                if not _is_nonnegative_integer(value.get(counter)):
                    raise ValueError("{} {}".format(field, counter))
            for diagnostic in (
                "worker_stdout_excerpt",
                "worker_stderr_excerpt",
            ):
                text = value.get(diagnostic)
                if not isinstance(text, str) or len(
                    text.encode("utf-8", "replace")
                ) > MAX_DIAGNOSTIC_BYTES:
                    raise ValueError("{} {}".format(field, diagnostic))


def _validate_class(
    value: object, index: int, *, require_complete: bool
) -> None:
    if type(value) is not dict or set(value) != CLASS_FIELDS:
        raise ValueError("class fields")
    corpus = value["corpus"]
    if (
        type(corpus) is not dict
        or index >= len(CASES)
        or _canonical_document(corpus)
        != _canonical_document(dict(CASES[index]))
    ):
        raise ValueError("class corpus")
    if any(
        not _is_nonnegative_integer(corpus.get(field))
        for field in _CASE_INTEGER_FIELDS
    ):
        raise ValueError("class corpus integer fields")
    if value["name"] != corpus["name"]:
        raise ValueError("class name")
    if type(value["samples"]) is not list:
        raise ValueError("class samples")
    if type(value["gates"]) is not dict or set(value["gates"]) != set(GATE_NAMES):
        raise ValueError("class gates")
    if any(type(item) is not bool for item in value["gates"].values()):
        raise ValueError("class gate values")
    if type(value["corpus_checks"]) is not dict or set(value["corpus_checks"]) != {
        "fixture_preflight_completed",
        "warmup_has_no_correctness_failure",
        "samples_have_no_correctness_failure",
        "descriptor_capability_and_overflow_counts_exact",
        "equivalent_permutations_byte_identical",
    }:
        raise ValueError("class checks")
    if any(type(item) is not bool for item in value["corpus_checks"].values()):
        raise ValueError("class check values")
    if type(value["correctness_passed"]) is not bool or type(
        value["mandatory_gates_passed"]
    ) is not bool:
        raise ValueError("class decisions")

    if value["status"] == "class-error":
        if require_complete:
            raise ValueError("incomplete class")
        if value["warmup"] is not None or value["samples"] != []:
            raise ValueError("failed class retention")
        if (
            not isinstance(value["error"], str)
            or not value["error"]
            or len(value["error"].encode("utf-8", "replace"))
            > MAX_DIAGNOSTIC_BYTES
        ):
            raise ValueError("failed class error")
        if any(
            value[field] != {}
            for field in (
                "wall_time",
                "cpu_time",
                "peak_rss_bytes",
                "input_bytes",
                "output_bytes",
                "counts",
                "forbidden_counter_totals",
            )
        ):
            raise ValueError("failed class summaries")
        if value["performance_failures"] != []:
            raise ValueError("failed class performance failures")
        if any(value["corpus_checks"].values()) or any(value["gates"].values()):
            raise ValueError("failed class flags")
        if value["correctness_passed"] or value["mandatory_gates_passed"]:
            raise ValueError("failed class decisions")
        return
    if value["status"] != "ok" or value["error"] is not None:
        raise ValueError("class status")
    _validate_sample_shape(value["warmup"], corpus, "class warmup", 0)
    for sample_index, sample in enumerate(value["samples"]):
        _validate_sample_shape(
            sample,
            corpus,
            "class sample {}".format(sample_index),
            sample_index + 1,
        )
    complete = (
        value["warmup"].get("status") == "ok"
        and value["warmup"].get("correctness_passed") is True
        and value["warmup"].get("correctness_failure") is False
        and len(value["samples"]) == MEASURED_RUNS
        and all(
            sample.get("status") == "ok"
            and sample.get("correctness_passed") is True
            and sample.get("correctness_failure") is False
            and sample.get("performance_failure") is False
            for sample in value["samples"]
        )
    )
    if require_complete and not complete:
        raise ValueError("class retained samples incomplete")
    for group, expected_keys in (
        (
            value["wall_time"],
            {
                "cold_seconds",
                "warm_seconds",
                "discovery_seconds",
                "routing_seconds",
                "consent_lookup_decision_seconds",
            },
        ),
        (value["cpu_time"], {"cold_seconds", "warm_seconds"}),
        (
            value["input_bytes"],
            {
                "host_inventory_bytes",
                "user_registry_bytes",
                "registration_bytes",
                "consent_bytes",
                "request_bytes",
                "discovery_input_bytes",
                "routing_input_bytes",
            },
        ),
        (
            value["output_bytes"],
            {
                "discovery_output_bytes",
                "routing_output_bytes",
                "discovery_artifact_bytes",
                "model_summary_characters",
            },
        ),
        (
            value["counts"],
            {
                "corpus_provider_count",
                "external_provider_count",
                "capabilities_per_external_provider",
                "source_descriptor_count",
                "discovered_descriptor_count",
                "discovered_capability_count",
                "rejected_provider_count",
                "omitted_provider_count",
            },
        ),
    ):
        if type(group) is not dict or set(group) != expected_keys:
            raise ValueError("class summary group")
        for name, summary in group.items():
            _validate_summary(summary, str(name))
    _validate_summary(value["peak_rss_bytes"], "peak_rss_bytes")

    expected = _assemble_class(corpus, value["warmup"], value["samples"])
    if _canonical_document(value) != _canonical_document(expected):
        raise ValueError("class derived evidence")


def _validate_evidence_document(value: object, *, require_complete: bool) -> None:
    if type(value) is not dict or set(value) != EVIDENCE_FIELDS:
        raise ValueError("evidence fields")
    if (
        value["schema"] != SCHEMA
        or type(value["seed"]) is not int
        or value["seed"] != SEED
    ):
        raise ValueError("evidence identity")
    if (
        type(value["warm_up_runs_per_class"]) is not int
        or value["warm_up_runs_per_class"] != WARM_UP_RUNS
        or type(value["measured_runs_per_class"]) is not int
        or value["measured_runs_per_class"] != MEASURED_RUNS
    ):
        raise ValueError("evidence run counts")
    if value["percentile_method"] != "nearest-rank":
        raise ValueError("evidence percentile")
    if (
        type(value["timing_definitions"]) is not dict
        or _canonical_document(value["timing_definitions"])
        != _canonical_document(TIMING_DEFINITIONS)
    ):
        raise ValueError("evidence timing definitions")
    if (
        type(value["thresholds"]) is not dict
        or _canonical_document(value["thresholds"])
        != _canonical_document(THRESHOLDS)
    ):
        raise ValueError("evidence thresholds")
    if type(value["machine"]) is not dict or set(value["machine"]) != set(
        MACHINE_FIELDS
    ):
        raise ValueError("evidence machine fields")
    for field in MACHINE_FIELDS:
        machine_value = value["machine"][field]
        if field == "cpu_count":
            if machine_value is not None and not _is_nonnegative_integer(
                machine_value
            ):
                raise ValueError("evidence machine cpu_count")
        elif not isinstance(machine_value, str) or len(
            machine_value.encode("utf-8", "replace")
        ) > MAX_DIAGNOSTIC_BYTES:
            raise ValueError("evidence machine {}".format(field))
    if value["machine"]["git"] != "not invoked by pure benchmark":
        raise ValueError("evidence machine git")
    if (
        type(value["classes"]) is not list
        or len(value["classes"]) != len(CASES)
    ):
        raise ValueError("evidence classes")
    for index, item in enumerate(value["classes"]):
        _validate_class(item, index, require_complete=require_complete)
    if type(value["correctness_passed"]) is not bool or type(
        value["mandatory_gates_passed"]
    ) is not bool:
        raise ValueError("evidence decision flags")
    aggregate_correctness = all(
        item["correctness_passed"] is True for item in value["classes"]
    )
    aggregate_gates = all(
        item["mandatory_gates_passed"] is True for item in value["classes"]
    )
    if (
        value["correctness_passed"] is not aggregate_correctness
        or value["mandatory_gates_passed"] is not aggregate_gates
    ):
        raise ValueError("evidence aggregate flags")
    expected_decision = _retention_decision(
        value["correctness_passed"], value["mandatory_gates_passed"]
    )
    if value["python_retention_decision"] != expected_decision:
        raise ValueError("evidence decision")
    # The encoder below rejects non-finite values at every nested depth.
    json.dumps(value, allow_nan=False)


def _validate_evidence(value: object) -> None:
    _validate_evidence_document(value, require_complete=True)


def _validate_partial_evidence(value: object) -> None:
    _validate_evidence_document(value, require_complete=False)


def _canonical_document(value: Mapping[str, object]) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _existing_complete_evidence(path: Path) -> bool:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=lambda item: (_ for _ in ()).throw(
                ValueError("non-finite {}".format(item))
            ),
        )
        _validate_evidence(value)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return True


def _class_is_complete(value: Mapping[str, object]) -> bool:
    warmup = value.get("warmup")
    samples = value.get("samples")
    return (
        value.get("status") == "ok"
        and isinstance(warmup, Mapping)
        and warmup.get("status") == "ok"
        and warmup.get("correctness_passed") is True
        and warmup.get("correctness_failure") is False
        and isinstance(samples, list)
        and len(samples) == MEASURED_RUNS
        and all(
            isinstance(sample, Mapping)
            and sample.get("status") == "ok"
            and sample.get("correctness_passed") is True
            and sample.get("correctness_failure") is False
            and sample.get("performance_failure") is False
            for sample in samples
        )
    )


def _write_atomic(output: Path, evidence: Mapping[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(".{}.tmp-{}".format(output.name, os.getpid()))
    try:
        with temporary.open("x", encoding="utf-8") as destination:
            destination.write(_canonical_document(evidence))
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(str(temporary), str(output))
        directory = os.open(str(output.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _driver(output: Path) -> int:
    output = output.resolve(strict=False)
    classes = []
    for case in CASES:
        try:
            result = _case_result(case)
        except Exception as exc:  # Later class failures must remain evidence.
            result = _failed_class(case, exc)
        classes.append(result)
    evidence = _evidence(classes, machine=_machine())
    partial = not all(_class_is_complete(item) for item in classes)
    if partial:
        _validate_partial_evidence(evidence)
    else:
        _validate_evidence(evidence)
    preserved = partial and _existing_complete_evidence(output)
    if not preserved:
        _write_atomic(output, evidence)

    summary = {
        "correctness_passed": evidence["correctness_passed"],
        "mandatory_gates_passed": evidence["mandatory_gates_passed"],
        "output": str(output),
        "python_retention_decision": evidence["python_retention_decision"],
    }
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return 0 if evidence["correctness_passed"] and evidence[
        "mandatory_gates_passed"
    ] else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure deterministic context discovery and routing gates."
    )
    parser.add_argument("--output", type=Path, help="explicit private raw JSON path")
    parser.add_argument("--worker-case", help=argparse.SUPPRESS)
    parser.add_argument("--worker-run", type=int, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker_case is not None:
        if args.output is not None or args.worker_run is None or args.worker_run < 0:
            raise SystemExit("invalid benchmark worker arguments")
        matches = [case for case in CASES if case["name"] == args.worker_case]
        if len(matches) != 1:
            raise SystemExit("unknown benchmark worker case")
        return _worker(matches[0], args.worker_run)
    if args.output is None:
        _parser().error("--output is required")
    if args.worker_run is not None:
        raise SystemExit("incomplete benchmark worker arguments")
    return _driver(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
