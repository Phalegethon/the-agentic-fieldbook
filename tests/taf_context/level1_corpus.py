"""Deterministic, redistributable corpus data for Level 1 conformance tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping, Optional


class CorpusClass(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


CORPUS_SIZES = {
    CorpusClass.SMALL: 200,
    CorpusClass.MEDIUM: 25_000,
    CorpusClass.LARGE: 100_000,
}
LANGUAGE_WEIGHTS = {
    "Python": 24,
    "TypeScript": 20,
    "JavaScript": 16,
    "Go": 16,
    "Rust": 16,
    "Markdown": 8,
}
SCENARIO_ROOTS = (
    "scenarios/01-clean-python",
    "scenarios/02-mixed-monorepo",
    "scenarios/03-long-document",
    "generated",
    "scenarios/05-dirty-refactor",
    "scenarios/06-committed-changes",
    "scenarios/07-linked-worktree",
    "scenarios/08-unsupported-language",
    "scenarios/09-corrupt-index",
    "scenarios/10-moved-identity",
)
MUTATION_IDENTITIES = (
    "committed-add-modify-rename-delete",
    "corrupt-index-sentinel",
    "dirty-refactor-100",
    "moved-identity",
)
_MANIFEST_FIELDS = {
    "schema_version",
    "seed",
    "corpus_class",
    "repository_relative_roots",
    "first_party_file_count",
    "relevant_source_bytes",
    "language_counts",
    "exclusion_reason_counts",
    "long_document_path",
    "expected_record_identities",
    "mutation_identities",
}
_MUTATION_FIELDS = {
    "schema_version",
    "mutation_identity",
    "added_paths",
    "modified_paths",
    "renamed_paths",
    "deleted_paths",
    "before_tree",
    "after_tree",
    "dirty_overlay_fingerprint",
    "expected_record_identities",
    "forbidden_record_identities",
}
_LANGUAGE_EXTENSIONS = {
    "Python": "py",
    "TypeScript": "ts",
    "JavaScript": "js",
    "Go": "go",
    "Rust": "rs",
    "Markdown": "md",
}


@dataclass(frozen=True)
class CorpusManifest:
    schema_version: str
    seed: int
    corpus_class: CorpusClass
    repository_relative_roots: tuple[str, ...]
    first_party_file_count: int
    relevant_source_bytes: int
    language_counts: Mapping[str, int]
    exclusion_reason_counts: Mapping[str, int]
    long_document_path: str
    expected_record_identities: tuple[str, ...]
    mutation_identities: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "CorpusManifest":
        _exact_fields(value, _MANIFEST_FIELDS)
        schema_version = _schema(value)
        seed = _integer(value, "seed", minimum=0)
        try:
            corpus_class = CorpusClass(value["corpus_class"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("invalid corpus_class") from error
        roots = _paths(value, "repository_relative_roots")
        first_party = _integer(value, "first_party_file_count", minimum=1)
        source_bytes = _integer(value, "relevant_source_bytes", minimum=1)
        language_counts = _counts(value, "language_counts", set(LANGUAGE_WEIGHTS))
        exclusion_counts = _counts(value, "exclusion_reason_counts")
        long_document_path = _path(value, "long_document_path")
        expected = _identities(value, "expected_record_identities")
        mutations = _sorted_strings(value, "mutation_identities")
        if roots != SCENARIO_ROOTS:
            raise ValueError("invalid repository_relative_roots")
        if tuple(mutations) != MUTATION_IDENTITIES:
            raise ValueError("invalid mutation_identities")
        if sum(language_counts.values()) != first_party:
            raise ValueError("invalid language_counts")
        if first_party + sum(exclusion_counts.values()) != CORPUS_SIZES[corpus_class]:
            raise ValueError("invalid corpus size accounting")
        return cls(
            schema_version,
            seed,
            corpus_class,
            roots,
            first_party,
            source_bytes,
            MappingProxyType(language_counts),
            MappingProxyType(exclusion_counts),
            long_document_path,
            expected,
            mutations,
        )

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> "CorpusManifest":
        return cls.from_dict(_parse_json_object(raw))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "seed": self.seed,
            "corpus_class": self.corpus_class.value,
            "repository_relative_roots": list(self.repository_relative_roots),
            "first_party_file_count": self.first_party_file_count,
            "relevant_source_bytes": self.relevant_source_bytes,
            "language_counts": dict(self.language_counts),
            "exclusion_reason_counts": dict(self.exclusion_reason_counts),
            "long_document_path": self.long_document_path,
            "expected_record_identities": list(self.expected_record_identities),
            "mutation_identities": list(self.mutation_identities),
        }

    def to_json_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True)
class MutationManifest:
    schema_version: str
    mutation_identity: str
    added_paths: tuple[str, ...]
    modified_paths: tuple[str, ...]
    renamed_paths: tuple[tuple[str, str], ...]
    deleted_paths: tuple[str, ...]
    before_tree: str
    after_tree: str
    dirty_overlay_fingerprint: str
    expected_record_identities: tuple[str, ...]
    forbidden_record_identities: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "MutationManifest":
        _exact_fields(value, _MUTATION_FIELDS)
        mutation_identity = _string(value, "mutation_identity")
        if mutation_identity not in MUTATION_IDENTITIES:
            raise ValueError("invalid mutation_identity")
        renamed_raw = value.get("renamed_paths")
        if type(renamed_raw) is not list:
            raise ValueError("invalid renamed_paths")
        renamed: list[tuple[str, str]] = []
        for pair in renamed_raw:
            if type(pair) is not list or len(pair) != 2:
                raise ValueError("invalid renamed_paths")
            renamed.append((_path_value(pair[0]), _path_value(pair[1])))
        renamed_tuple = tuple(renamed)
        if renamed_tuple != tuple(sorted(renamed_tuple)):
            raise ValueError("invalid renamed_paths")
        return cls(
            _schema(value),
            mutation_identity,
            _paths(value, "added_paths"),
            _paths(value, "modified_paths"),
            renamed_tuple,
            _paths(value, "deleted_paths"),
            _object_id(value, "before_tree"),
            _object_id(value, "after_tree"),
            _sha256(value, "dirty_overlay_fingerprint"),
            _identities(value, "expected_record_identities"),
            _identities(value, "forbidden_record_identities"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mutation_identity": self.mutation_identity,
            "added_paths": list(self.added_paths),
            "modified_paths": list(self.modified_paths),
            "renamed_paths": [list(pair) for pair in self.renamed_paths],
            "deleted_paths": list(self.deleted_paths),
            "before_tree": self.before_tree,
            "after_tree": self.after_tree,
            "dirty_overlay_fingerprint": self.dirty_overlay_fingerprint,
            "expected_record_identities": list(self.expected_record_identities),
            "forbidden_record_identities": list(self.forbidden_record_identities),
        }

    def to_json_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())


@dataclass(frozen=True)
class _Record:
    identity: str
    path: str
    start_line: int
    end_line: int
    language: str
    qualified_name: str
    content: bytes


def generate_level1_corpus(
    root: Path,
    corpus_class: CorpusClass,
    seed: int = 20260827,
) -> CorpusManifest:
    """Generate an exact-size corpus without using the absolute root as data."""
    if not isinstance(root, Path) or type(seed) is not int or seed < 0:
        raise ValueError("invalid corpus arguments")
    if not isinstance(corpus_class, CorpusClass):
        raise ValueError("invalid corpus_class")
    root.mkdir(parents=True, exist_ok=True)
    if any(path.is_file() for path in root.rglob("*") if ".git" not in path.parts):
        raise ValueError("corpus root is not empty")

    total = CORPUS_SIZES[corpus_class]
    excluded_generated_vendor = int(total * 0.55)
    binary_oversized = 20
    first_party_count = total - excluded_generated_vendor - binary_oversized
    language_counts = _allocate_languages(first_party_count)
    records = _build_first_party_records(seed, language_counts)
    for record in records:
        _write(root, record.path, record.content)

    exclusion_counts = _write_excluded(
        root,
        seed,
        excluded_generated_vendor,
        binary_oversized,
    )
    relevant_source_bytes = sum(len(record.content) for record in records)
    long_document = next(
        record.path for record in records if record.path.endswith("structured-guide.md")
    )
    manifest = CorpusManifest.from_dict(
        {
            "schema_version": "1",
            "seed": seed,
            "corpus_class": corpus_class.value,
            "repository_relative_roots": list(SCENARIO_ROOTS),
            "first_party_file_count": first_party_count,
            "relevant_source_bytes": relevant_source_bytes,
            "language_counts": language_counts,
            "exclusion_reason_counts": exclusion_counts,
            "long_document_path": long_document,
            "expected_record_identities": sorted(
                record.identity for record in _conformance_records(records)
            ),
            "mutation_identities": list(MUTATION_IDENTITIES),
        }
    )
    actual_count = sum(
        1
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    if actual_count != total:
        raise RuntimeError("generated corpus file count mismatch")
    return manifest


def apply_mutation(
    repo: Path,
    manifest: CorpusManifest,
    mutation_identity: str,
) -> MutationManifest:
    """Apply one declared deterministic mutation to a generated Git corpus."""
    if mutation_identity not in manifest.mutation_identities:
        raise ValueError("unknown mutation_identity")
    before_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    records = _records_from_repo(repo, manifest)
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()
    deleted: tuple[str, ...] = ()
    expected: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()

    if mutation_identity == "committed-add-modify-rename-delete":
        selected = records[:3]
        if len(selected) != 3:
            raise ValueError("corpus cannot support mutation")
        added = ("scenarios/06-committed-changes/added_service.py",)
        modified = (selected[0].path,)
        original = PurePosixPath(selected[1].path)
        renamed_path = str(
            original.with_name(f"{original.stem}_renamed{original.suffix}")
        )
        renamed = ((selected[1].path, renamed_path),)
        deleted = (selected[2].path,)
        added_record = _record_for_custom_path(
            manifest.seed,
            added[0],
            "AddedService",
            "return 'added'",
        )
        modified_record = _record_for_custom_path(
            manifest.seed,
            modified[0],
            "ModifiedService",
            "return 'modified'",
        )
        renamed_source = repo / selected[1].path
        renamed_target = repo / renamed[0][1]
        renamed_target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(renamed_source, renamed_target)
        (repo / deleted[0]).unlink()
        _write(repo, added_record.path, added_record.content)
        _write(repo, modified_record.path, modified_record.content)
        renamed_record = _record_identity(
            renamed[0][1],
            selected[1].start_line,
            selected[1].end_line,
            "definition",
            selected[1].qualified_name,
        )
        expected = tuple(sorted((added_record.identity, modified_record.identity, renamed_record)))
        forbidden = tuple(sorted(item.identity for item in selected))
    elif mutation_identity == "dirty-refactor-100":
        if len(records) < 100:
            raise ValueError("dirty-refactor-100 requires medium or large corpus")
        chosen = records[:100]
        for ordinal, record in enumerate(chosen):
            comment = "#" if record.language in {"Python", "Markdown"} else "//"
            content = record.content + f"{comment} dirty-refactor-{ordinal:03d}\n".encode("ascii")
            _write(repo, record.path, content)
        modified = tuple(sorted(record.path for record in chosen))
        expected = tuple(sorted(record.identity for record in chosen))
    elif mutation_identity == "corrupt-index-sentinel":
        added = ("scenarios/09-corrupt-index/runtime-corrupt.marker",)
        _write(repo, added[0], b"TAF_CORRUPT_INDEX_SENTINEL\n")
    elif mutation_identity == "moved-identity":
        added = ("scenarios/10-moved-identity/runtime-moved.marker",)
        _write(repo, added[0], b"TAF_MOVED_IDENTITY_SENTINEL\n")

    _git(repo, "add", "-A")
    after_tree = _git(repo, "write-tree")
    overlay = _git_bytes(repo, "diff", "--cached", "--binary", "HEAD")
    fingerprint = "sha256:" + hashlib.sha256(overlay).hexdigest()
    return MutationManifest.from_dict(
        {
            "schema_version": "1",
            "mutation_identity": mutation_identity,
            "added_paths": list(added),
            "modified_paths": list(modified),
            "renamed_paths": [list(pair) for pair in renamed],
            "deleted_paths": list(deleted),
            "before_tree": before_tree,
            "after_tree": after_tree,
            "dirty_overlay_fingerprint": fingerprint,
            "expected_record_identities": list(expected),
            "forbidden_record_identities": list(forbidden),
        }
    )


def expected_records(
    corpus_class: CorpusClass = CorpusClass.SMALL,
    seed: int = 20260827,
) -> tuple[dict[str, object], ...]:
    """Return canonical record metadata used to author conformance vectors."""
    total = CORPUS_SIZES[corpus_class]
    first_party = total - int(total * 0.55) - 20
    records = _build_first_party_records(seed, _allocate_languages(first_party))
    return tuple(
        {
            "result_identity": record.identity,
            "path": record.path,
            "start_line": record.start_line,
            "end_line": record.end_line,
            "language": record.language,
            "qualified_name": record.qualified_name,
        }
        for record in records
    )


def _conformance_records(records: tuple[_Record, ...]) -> tuple[_Record, ...]:
    by_language = {
        language: tuple(record for record in records if record.language == language)
        for language in LANGUAGE_WEIGHTS
    }
    selected = (
        by_language["Python"][:17]
        + by_language["TypeScript"][:7]
        + by_language["Markdown"][:6]
    )
    if len(selected) != 30:
        raise ValueError("corpus lacks required conformance records")
    return selected


def _build_first_party_records(seed: int, counts: Mapping[str, int]) -> tuple[_Record, ...]:
    records: list[_Record] = []
    for language in LANGUAGE_WEIGHTS:
        for ordinal in range(counts[language]):
            if language == "Markdown" and ordinal == 0:
                path = "scenarios/03-long-document/structured-guide.md"
                content, start, end, qualified = _long_document(seed)
            else:
                path = _source_path(language, ordinal)
                content, start, end, qualified = _source_template(language, seed, ordinal)
            identity = _record_identity(path, start, end, "definition", qualified)
            records.append(_Record(identity, path, start, end, language, qualified, content))
    return tuple(records)


def _source_path(language: str, ordinal: int) -> str:
    lane = language.lower().replace("script", "script")
    extension = _LANGUAGE_EXTENSIONS[language]
    if language == "Python" and ordinal < 6:
        root = "scenarios/01-clean-python"
    elif ordinal % 5 == 0:
        root = "scenarios/05-dirty-refactor"
    elif ordinal % 5 == 1:
        root = "scenarios/06-committed-changes"
    elif ordinal % 5 == 2:
        root = "scenarios/07-linked-worktree"
    else:
        root = "scenarios/02-mixed-monorepo"
    return f"{root}/{lane}/pkg_{ordinal:05d}.{extension}"


def _source_template(language: str, seed: int, ordinal: int) -> tuple[bytes, int, int, str]:
    short = f"Service_{ordinal % 7:04d}"
    qualified = f"pkg_{ordinal:05d}.{short}"
    marker = f"synthetic-{seed}-{language.lower()}-{ordinal:05d}"
    if language == "Python":
        lines = [
            f'"""{marker}."""',
            "from shared.runtime import Registry",
            "",
            f"class {short}:",
            "    def run(self):",
            f'        return "{marker}"',
        ]
        start, end = 4, 6
    elif language == "TypeScript":
        lines = [
            'import { Registry } from "@shared/runtime";',
            "",
            f"export class {short} {{",
            f'  run(): string {{ return "{marker}"; }}',
            "}",
        ]
        start, end = 3, 5
    elif language == "JavaScript":
        lines = [
            'import { Registry } from "@shared/runtime";',
            "",
            f"export class {short} {{",
            f'  run() {{ return "{marker}"; }}',
            "}",
        ]
        start, end = 3, 5
    elif language == "Go":
        lines = [
            f"package pkg_{ordinal:05d}",
            "",
            'import "example.invalid/shared/runtime"',
            "",
            f"type {short} struct {{}}",
            f'func (service {short}) Run() string {{ return "{marker}" }}',
        ]
        start, end = 5, 6
    elif language == "Rust":
        lines = [
            "use crate::shared::runtime::Registry;",
            "",
            f"pub struct {short};",
            f"impl {short} {{",
            f'    pub fn run(&self) -> &str {{ "{marker}" }}',
            "}",
        ]
        start, end = 3, 6
    else:
        lines = [
            f"# {short}",
            "",
            marker,
            "",
            f"Configuration key: synthetic.feature.{ordinal:05d}",
        ]
        start, end = 1, 5
    lines.extend(_lane_extras(language, ordinal, short))
    return ("\n".join(lines) + "\n").encode("utf-8"), start, end, qualified


def _lane_extras(language: str, ordinal: int, short: str) -> list[str]:
    comment = "#" if language == "Python" else "//"
    if language == "Markdown":
        comment = "<!--"
    marker_suffix = " -->" if language == "Markdown" else ""
    extras: list[str] = []
    if ordinal == 0:
        extras.append(f"{comment} TAF_ENTRY_POINT{marker_suffix}")
        if language == "Python":
            extras.extend(("if __name__ == \"__main__\":", f"    {short}().run()"))
        elif language in {"TypeScript", "JavaScript"}:
            extras.append(f"export default new {short}();")
        elif language == "Go":
            extras.append(f"func EntryPoint() string {{ return {short}{{}}.Run() }}")
        elif language == "Rust":
            extras.append(f"pub fn entry_point() -> &'static str {{ {short}.run() }}")
    if ordinal == 1:
        extras.append(f"{comment} TAF_CONFIG_KEY synthetic.feature.{ordinal:05d}{marker_suffix}")
    if ordinal == 2 or (language == "Python" and ordinal == 9):
        extras.append(f"{comment} TAF_DYNAMIC_RELATION runtime-selected-service{marker_suffix}")
        if language == "Python":
            extras.append('dynamic_service = getattr(Registry, "runtime_selected_service", None)')
        elif language in {"TypeScript", "JavaScript"}:
            extras.append("const dynamicService = Registry[runtimeSelectedService];")
        elif language == "Go":
            extras.append("// reflect.ValueOf(Registry{}).MethodByName(runtimeSelectedService)")
        elif language == "Rust":
            extras.append("// registry.lookup_dynamic(runtime_selected_service)")
    return extras


def _long_document(seed: int) -> tuple[bytes, int, int, str]:
    lines = [f"Synthetic handbook line {ordinal:05d} seed {seed}" for ordinal in range(1, 10_001)]
    lines[0] = "<!-- TAF_ENTRY_POINT -->"
    lines[4_998] = "## Recovery checkpoint protocol"
    lines[4_999] = "Use bounded evidence and repository-relative citations."
    lines[5_000] = "Never place the complete repository into model context."
    return ("\n".join(lines) + "\n").encode("utf-8"), 4_999, 5_001, "Recovery checkpoint protocol"


def _write_excluded(root: Path, seed: int, generated_vendor: int, binary_oversized: int) -> dict[str, int]:
    if generated_vendor < 7 or binary_oversized != 20:
        raise ValueError("corpus allocation is too small")
    counts = {
        "binary": 10,
        "candidate-state-sentinel": 2,
        "generated": (generated_vendor - 7) // 2,
        "ignore-rule": 1,
        "ignored-secret": 3,
        "oversized": 10,
        "unsupported-language": 1,
    }
    counts["vendor"] = generated_vendor - sum(
        counts[key]
        for key in (
            "candidate-state-sentinel",
            "generated",
            "ignore-rule",
            "ignored-secret",
            "unsupported-language",
        )
    )
    for ordinal in range(counts["generated"]):
        _write(root, f"generated/cache/generated_{ordinal:06d}.txt", f"generated {seed} {ordinal}\n".encode("ascii"))
    for ordinal in range(counts["vendor"]):
        _write(root, f"vendor/dependency/vendor_{ordinal:06d}.txt", f"vendor {seed} {ordinal}\n".encode("ascii"))
    for ordinal in range(counts["ignored-secret"]):
        _write(root, f"generated/ignored/secret_{ordinal}.env", f"TOKEN=TAF_CANARY_FAKE_ONLY_{seed}_{ordinal}\n".encode("ascii"))
    _write(root, ".gitignore", b"generated/ignored/\n")
    _write(root, "scenarios/08-unsupported-language/sample.zig", b"const dynamic = @import(\"runtime\");\n")
    _write(root, "scenarios/09-corrupt-index/corrupt-index.sentinel", b"CORRUPT_INDEX_FIXTURE_ONLY\n")
    _write(root, "scenarios/10-moved-identity/moved-identity.sentinel", b"MOVED_IDENTITY_FIXTURE_ONLY\n")
    for ordinal in range(counts["binary"]):
        payload = hashlib.sha256(f"{seed}:binary:{ordinal}".encode("ascii")).digest()
        _write(root, f"fixtures/binary/file_{ordinal:02d}.bin", payload)
    oversized_payload = (f"oversized fixture seed {seed}\n".encode("ascii") * 20_000)[:600_000]
    for ordinal in range(counts["oversized"]):
        _write(root, f"fixtures/oversized/file_{ordinal:02d}.txt", oversized_payload)
    return dict(sorted(counts.items()))


def _records_from_repo(repo: Path, manifest: CorpusManifest) -> tuple[_Record, ...]:
    canonical = _build_first_party_records(manifest.seed, manifest.language_counts)
    return tuple(record for record in canonical if (repo / record.path).is_file())


def _record_for_custom_path(seed: int, path: str, name: str, body: str) -> _Record:
    content = f'class {name}:\n    def run(self):\n        {body}\n'.encode("utf-8")
    identity = _record_identity(path, 1, 3, "definition", name)
    return _Record(identity, path, 1, 3, "Python", name, content)


def _record_identity(path: str, start: int, end: int, kind: str, qualified_name: str) -> str:
    canonical = f"1\0{path}\0{start}\0{end}\0{kind}\0{qualified_name}".encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _allocate_languages(total: int) -> dict[str, int]:
    allocated = {language: total * weight // 100 for language, weight in LANGUAGE_WEIGHTS.items()}
    remainder = total - sum(allocated.values())
    fractions = sorted(
        LANGUAGE_WEIGHTS,
        key=lambda language: (-(total * LANGUAGE_WEIGHTS[language] % 100), list(LANGUAGE_WEIGHTS).index(language)),
    )
    for language in fractions[:remainder]:
        allocated[language] += 1
    return dict(sorted(allocated.items()))


def _write(root: Path, relative: str, content: bytes) -> None:
    _path_value(relative)
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def _git(repo: Path, *arguments: str) -> str:
    return _git_bytes(repo, *arguments).decode("ascii").strip()


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    environment = os.environ.copy()
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0"})
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
    ).stdout


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _parse_json_object(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or len(raw) > 256 * 1024:
        raise ValueError("invalid manifest bytes")
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate manifest key")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite manifest number")))
    if type(value) is not dict:
        raise ValueError("invalid manifest object")
    return value


def _exact_fields(value: object, expected: set[str]) -> None:
    if type(value) is not dict or set(value) != expected:
        raise ValueError("invalid manifest fields")


def _schema(value: Mapping[str, object]) -> str:
    if value.get("schema_version") != "1":
        raise ValueError("invalid schema_version")
    return "1"


def _integer(value: Mapping[str, object], field: str, minimum: int) -> int:
    item = value.get(field)
    if type(item) is not int or item < minimum:
        raise ValueError(f"invalid {field}")
    return item


def _string(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if type(item) is not str or not item or len(item) > 512:
        raise ValueError(f"invalid {field}")
    return item


def _path(value: Mapping[str, object], field: str) -> str:
    return _path_value(value.get(field))


def _path_value(item: object) -> str:
    if type(item) is not str or not item or len(item) > 512 or "\\" in item:
        raise ValueError("invalid path")
    parsed = PurePosixPath(item)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("invalid path")
    return item


def _paths(value: Mapping[str, object], field: str) -> tuple[str, ...]:
    raw = value.get(field)
    if type(raw) is not list:
        raise ValueError(f"invalid {field}")
    result = tuple(_path_value(item) for item in raw)
    if len(result) != len(set(result)):
        raise ValueError(f"invalid {field}")
    return result


def _sorted_strings(value: Mapping[str, object], field: str) -> tuple[str, ...]:
    raw = value.get(field)
    if type(raw) is not list or not all(type(item) is str and item for item in raw):
        raise ValueError(f"invalid {field}")
    result = tuple(raw)
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ValueError(f"invalid {field}")
    return result


def _counts(
    value: Mapping[str, object],
    field: str,
    exact_keys: Optional[set[str]] = None,
) -> dict[str, int]:
    raw = value.get(field)
    if type(raw) is not dict or (exact_keys is not None and set(raw) != exact_keys):
        raise ValueError(f"invalid {field}")
    result: dict[str, int] = {}
    for key, item in raw.items():
        if type(key) is not str or not key or type(item) is not int or item < 0:
            raise ValueError(f"invalid {field}")
        result[key] = item
    if list(result) != sorted(result):
        raise ValueError(f"invalid {field}")
    return result


def _identities(value: Mapping[str, object], field: str) -> tuple[str, ...]:
    raw = value.get(field)
    if type(raw) is not list:
        raise ValueError(f"invalid {field}")
    result = tuple(raw)
    if result != tuple(sorted(result)) or len(result) != len(set(result)):
        raise ValueError(f"invalid {field}")
    for item in result:
        _sha256_value(item)
    return result


def _sha256(value: Mapping[str, object], field: str) -> str:
    return _sha256_value(value.get(field))


def _sha256_value(item: object) -> str:
    if type(item) is not str or len(item) != 71 or not item.startswith("sha256:"):
        raise ValueError("invalid sha256 identity")
    try:
        int(item[7:], 16)
    except ValueError as error:
        raise ValueError("invalid sha256 identity") from error
    if item != item.lower():
        raise ValueError("invalid sha256 identity")
    return item


def _object_id(value: Mapping[str, object], field: str) -> str:
    item = value.get(field)
    if type(item) is not str or len(item) not in {40, 64}:
        raise ValueError(f"invalid {field}")
    try:
        int(item, 16)
    except ValueError as error:
        raise ValueError(f"invalid {field}") from error
    return item
