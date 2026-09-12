"""Versioned offline corpus admission and reproducible case identities.

This repository/sdist research tool is deliberately outside the runtime wheel.
Provenance hashes are integrity/identity records, not publisher authentication.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from payload_palette.ingress import decode_json_bytes
from payload_palette.models import ENVELOPE_NAMES

VERSION = 1
MAX_CASE_BYTES = 262_144
MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
DATA = Path(__file__).with_name("corpus_data")
COMPARISONS = ("valid", "single", "multiple", "invalid", "equivalent")
FAMILIES = (
    "control",
    "json",
    "utf8",
    "duplicates",
    "numbers",
    "nesting",
    "unicode",
    "envelope",
    "aliases",
    "base64",
    "data_url",
    "mime",
    "signature",
    "url",
    "limits",
    "multi_fault",
    "external_json",
)
PROFILE_KEYS = frozenset({"parts", "text", "part_bytes", "total_bytes", "input_bytes", "urlsafe"})


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def integer(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")
    return value


def chunk_schedule(chunks: tuple[int, ...]) -> tuple[int, ...]:
    if type(chunks) is not tuple or not 1 <= len(chunks) <= 8:
        raise ValueError("chunks must be a tuple of one to eight distinct sizes")
    for chunk in chunks:
        integer(chunk, "chunk", 1, MAX_CASE_BYTES)
    if len(set(chunks)) != len(chunks):
        raise ValueError("chunk sizes must be distinct")
    return chunks


@dataclass(frozen=True)
class CorpusConfig:
    seed: int = 20260908
    repeats: int = 4
    chunks: tuple[int, ...] = (1, 7, 64, 4096)
    smoke_modulus: int = 1
    max_cases: int = 10_000
    max_input_bytes: int = 256 * 1024 * 1024
    max_executions: int = 100_000
    max_processed_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        integer(self.seed, "seed", 0, 2**32 - 1)
        integer(self.repeats, "repeats", 1, 16)
        integer(self.smoke_modulus, "smoke_modulus", 1, 256)
        integer(self.max_cases, "max_cases", 1, 10_000)
        integer(self.max_input_bytes, "max_input_bytes", 1, 256 * 1024 * 1024)
        integer(self.max_executions, "max_executions", 1, 100_000)
        integer(self.max_processed_bytes, "max_processed_bytes", 1, 256 * 1024 * 1024)
        chunk_schedule(self.chunks)


@dataclass(frozen=True)
class CorpusCase:
    envelope: str
    family: str
    comparison: str
    mutation: str
    parameters: tuple[tuple[str, str | int], ...]
    seed_id: str
    seed_sha256: str
    source_id: str
    source_version: str
    raw: bytes
    profile: tuple[tuple[str, int], ...] = ()
    precedence: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "envelope",
            "family",
            "comparison",
            "mutation",
            "seed_id",
            "source_id",
            "source_version",
        ):
            value = getattr(self, name)
            if type(value) is not str or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}", value
            ):
                raise ValueError(f"invalid bounded {name}")
        if type(self.seed_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.seed_sha256):
            raise ValueError("invalid seed digest")
        if self.envelope not in ENVELOPE_NAMES or self.family not in FAMILIES:
            raise ValueError("unknown envelope or mutation family")
        if self.comparison not in COMPARISONS:
            raise ValueError("unknown comparison class")
        if type(self.raw) is not bytes or len(self.raw) > MAX_CASE_BYTES:
            raise ValueError("case bytes exceed the hard ceiling")
        for name in ("parameters", "profile", "precedence"):
            pairs = getattr(self, name)
            if type(pairs) is not tuple or len(pairs) > 32:
                raise ValueError("case metadata must be bounded immutable tuples")
            if any(type(pair) is not tuple or len(pair) != 2 for pair in pairs):
                raise ValueError("case metadata entries must be immutable pairs")
        for name, value in self.parameters:
            if type(name) is not str or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise ValueError("invalid parameter name")
            if type(value) is int:
                integer(value, "parameter", 0, 2**32 - 1)
            elif type(value) is not str or len(value) > 256 or not value.isascii():
                raise ValueError("invalid bounded parameter value")
        if len(dict(self.parameters)) != len(self.parameters):
            raise ValueError("duplicate parameters")
        for name, value in self.profile:
            if type(name) is not str:
                raise ValueError("invalid policy profile key")
            if name not in PROFILE_KEYS:
                raise ValueError("unknown policy profile field")
            integer(value, name, 0, MAX_CASE_BYTES)
            if name in ("parts", "input_bytes") and value < 1:
                raise ValueError("positive policy profile value required")
            if name == "urlsafe" and value not in (0, 1):
                raise ValueError("urlsafe flag must be zero or one")
        if len(dict(self.profile)) != len(self.profile):
            raise ValueError("duplicate policy profile fields")
        if any(type(code) is not str for pair in self.precedence for code in pair):
            raise ValueError("precedence codes must be exact strings")
        if self.precedence and (
            self.comparison != "multiple"
            or self.precedence != (("input_encoding", "invalid_json"),)
        ):
            raise ValueError("only the declared lexical multi-fault precedence is supported")

    @property
    def raw_sha256(self) -> str:
        return digest(self.raw)

    def descriptor(self) -> dict[str, Any]:
        return {
            "generator_version": VERSION,
            "envelope": self.envelope,
            "family": self.family,
            "comparison": self.comparison,
            "mutation": self.mutation,
            "parameters": [list(item) for item in self.parameters],
            "seed_id": self.seed_id,
            "seed_sha256": self.seed_sha256,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "license": "MIT",
            "raw_sha256": self.raw_sha256,
            "raw_bytes": len(self.raw),
            "profile": [list(item) for item in self.profile],
            "precedence": [list(item) for item in self.precedence],
        }

    @property
    def case_id(self) -> str:
        return digest(canonical(self.descriptor()))


def _fields(value: Any, names: set[str], name: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != names:
        raise ValueError(f"invalid {name} fields")
    return value


def external_seeds() -> tuple[dict[str, Any], ...]:
    """Validate the complete pinned subset before returning any seed."""
    with (DATA / "json_testsuite.json").open("rb") as handle:
        raw = handle.read(MAX_ARTIFACT_BYTES + 1)
    document = _fields(
        decode_json_bytes(raw, max_input_bytes=MAX_ARTIFACT_BYTES),
        {
            "version",
            "project",
            "commit",
            "source_url",
            "spdx",
            "copyright",
            "license_sha256",
            "selection",
            "transformation",
            "seeds",
        },
        "provenance",
    )
    if (
        type(document["version"]) is not int
        or document["version"] != VERSION
        or document["project"] != "nst/JSONTestSuite"
        or document["commit"] != "1ef36fa01286573e846ac449e8683f8833c5b26a"
        or document["source_url"] != "https://github.com/nst/JSONTestSuite"
        or document["spdx"] != "MIT"
        or document["copyright"] != "Copyright (c) 2016 Nicolas Seriot"
        or document["license_sha256"]
        != "8bd0e0578be788c617ea01d18b2a8146e3746ae50bddadc65a5f9d3aad08ad49"
    ):
        raise ValueError("unrecognized immutable provenance or license")
    with (DATA / "LICENSE.JSONTestSuite").open("rb") as handle:
        notice = handle.read(8193)
    if digest(notice) != document["license_sha256"]:
        raise ValueError("retained license notice hash mismatch")
    for field in ("selection", "transformation"):
        if type(document[field]) is not str or not 1 <= len(document[field]) <= 1024:
            raise ValueError("missing bounded provenance explanation")
    seeds = document["seeds"]
    if type(seeds) is not list or len(seeds) != 24:
        raise ValueError("expected exactly 24 independent seeds")
    names: set[str] = set()
    for seed in seeds:
        _fields(seed, {"path", "blob", "size", "sha256", "base64"}, "seed")
        name = seed["path"]
        if (
            type(name) is not str
            or len(name) > 256
            or not name.startswith("test_parsing/")
            or not name.endswith(".json")
            or name.count("/") != 1
            or ".." in name
            or "\\" in name
            or name in names
        ):
            raise ValueError("invalid or duplicate seed path")
        names.add(name)
        integer(seed["size"], "seed size", 0, MAX_CASE_BYTES)
        if type(seed["base64"]) is not str or len(seed["base64"]) > 4 * ((MAX_CASE_BYTES + 2) // 3):
            raise ValueError("seed encoding exceeds bound")
        try:
            value = base64.b64decode(seed["base64"], validate=True)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid seed base64") from exc
        blob = hashlib.sha1(
            b"blob " + str(len(value)).encode() + b"\0" + value, usedforsecurity=False
        ).hexdigest()
        if len(value) != seed["size"] or digest(value) != seed["sha256"] or blob != seed["blob"]:
            raise ValueError("seed artifact hash/size mismatch")
    for prefix in ("y_", "n_", "i_"):
        if sum(name.startswith("test_parsing/" + prefix) for name in names) != 8:
            raise ValueError("independent seed group cardinality changed")
    return tuple(seeds)


def iter_cases(config: CorpusConfig) -> Iterator[CorpusCase]:
    """Expand one bounded case at a time; public replay must preflight first."""
    from benchmarks.corpus_cases import authored_cases, foreign_cases

    if type(config) is not CorpusConfig:
        raise ValueError("config must be CorpusConfig")
    seeds = external_seeds()
    for repetition in range(config.repeats):
        for case in authored_cases(config.seed, repetition):
            if int(case.case_id[:8], 16) % config.smoke_modulus == 0:
                yield case
    for case in foreign_cases(seeds):
        if int(case.case_id[:8], 16) % config.smoke_modulus == 0:
            yield case


@dataclass(frozen=True)
class CorpusPlan:
    case_count: int
    input_bytes: int
    executions: int
    processed_bytes: int
    corpus_root: str
    config_sha256: str
    rules_sha256: str


def corpus_plan(config: CorpusConfig) -> CorpusPlan:
    """Finish admission and fingerprint all recipes before any normalization."""
    rules = hashlib.sha256()
    for name in (
        "corpus.py",
        "corpus_cases.py",
        "corpus_replay.py",
        "corpus_worker.py",
        "corpus_cli.py",
    ):
        with Path(__file__).with_name(name).open("rb") as handle:
            source = handle.read(1024 * 1024 + 1)
        if len(source) > 1024 * 1024:
            raise ValueError("generator source exceeds bound")
        rules.update(canonical([name, digest(source)]))
    for name in ("json_testsuite.json", "LICENSE.JSONTestSuite"):
        with (DATA / name).open("rb") as handle:
            source = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(source) > MAX_ARTIFACT_BYTES:
            raise ValueError("corpus artifact exceeds bound")
        rules.update(canonical([name, digest(source)]))
    config_hash = digest(canonical(asdict(config)))
    root = hashlib.sha256(canonical([VERSION, config_hash, rules.hexdigest()]))
    count = size = 0
    seen: set[str] = set()
    for case in iter_cases(config):
        count += 1
        size += len(case.raw)
        if count > config.max_cases:
            raise ValueError("case count exceeds declared budget")
        if size > config.max_input_bytes:
            raise ValueError("aggregate input bytes exceed declared budget")
        if count * (1 + len(config.chunks)) > config.max_executions:
            raise ValueError("path executions exceed declared budget")
        if size * (1 + len(config.chunks)) > config.max_processed_bytes:
            raise ValueError("processed bytes exceed declared budget")
        if case.case_id in seen:
            raise ValueError("duplicate derived case id")
        seen.add(case.case_id)
        root.update(canonical([case.case_id, case.descriptor()]))
    return CorpusPlan(
        count,
        size,
        count * (1 + len(config.chunks)),
        size * (1 + len(config.chunks)),
        root.hexdigest(),
        config_hash,
        rules.hexdigest(),
    )
