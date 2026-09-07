"""Bounded batch normalization and manifest comparison utilities.

This module is intentionally provider-neutral.  It makes Payload Palette
usable in offline dataset audits and queue preflight jobs while retaining the
same strict parser and policy for every record.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from payload_palette.errors import PayloadValidationError
from payload_palette.ingress import decode_json_bytes
from payload_palette.models import Manifest
from payload_palette.normalizer import normalize
from payload_palette.policy import NormalizationPolicy


@dataclass(frozen=True, slots=True)
class BatchRecord:
    """One deterministic input/output record from a batch run."""

    ordinal: int
    input_sha256: str
    manifest: Manifest | None = None
    errors: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.ordinal < 0 or len(self.input_sha256) != 64:
            raise ValueError("ordinal and input_sha256 are invalid")
        if (self.manifest is None) == (not self.errors):
            raise ValueError("a batch record carries either a manifest or errors")
        if any(not isinstance(item, Mapping) for item in self.errors):
            raise ValueError("errors must contain mappings")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ordinal": self.ordinal,
            "input_sha256": self.input_sha256,
            "valid": self.manifest is not None,
        }
        if self.manifest is not None:
            result["manifest"] = self.manifest.to_dict()
        else:
            result["errors"] = [dict(item) for item in self.errors]
        return result


@dataclass(frozen=True, slots=True)
class BatchReport:
    """Aggregate batch result with a reproducibility digest."""

    records: tuple[BatchRecord, ...]
    input_sha256: str
    valid_count: int
    invalid_count: int

    def __post_init__(self) -> None:
        if self.valid_count + self.invalid_count != len(self.records):
            raise ValueError("batch counts do not match records")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "input_sha256": self.input_sha256,
            "records": len(self.records),
            "valid": self.valid_count,
            "invalid": self.invalid_count,
            "items": [item.to_dict() for item in self.records],
        }


def _issues(error: PayloadValidationError) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "code": issue.code,
            "message": issue.message,
            "path": issue.path,
            **({"hint": issue.hint} if issue.hint is not None else {}),
        }
        for issue in error.issues
    )


def normalize_batch(
    documents: Iterable[Any],
    policy: NormalizationPolicy | None = None,
    *,
    max_records: int = 100_000,
) -> BatchReport:
    """Normalize records independently and retain all structured failures."""

    if type(max_records) is not int or not 1 <= max_records <= 1_000_000:
        raise ValueError("max_records must be between 1 and 1000000")
    active = policy or NormalizationPolicy()
    records: list[BatchRecord] = []
    canonical_inputs: list[bytes] = []
    for ordinal, document in enumerate(documents):
        if ordinal >= max_records:
            raise ValueError("batch exceeds max_records")
        encoded = json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        canonical_inputs.append(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        try:
            manifest = normalize(document, active)
        except PayloadValidationError as error:
            records.append(BatchRecord(ordinal, digest, errors=_issues(error)))
        else:
            records.append(BatchRecord(ordinal, digest, manifest=manifest))
    if not records:
        raise ValueError("batch must contain at least one record")
    input_digest = hashlib.sha256(b"\n".join(canonical_inputs)).hexdigest()
    valid = sum(item.manifest is not None for item in records)
    return BatchReport(tuple(records), input_digest, valid, len(records) - valid)


def normalize_jsonl(
    path: str | Path,
    policy: NormalizationPolicy | None = None,
    *,
    max_records: int = 100_000,
) -> BatchReport:
    """Normalize one JSONL file without buffering the entire input list."""

    source = Path(path)

    def rows() -> Iterator[Any]:
        for line_number, line in enumerate(source.read_bytes().splitlines(), start=1):
            if line.strip():
                try:
                    yield decode_json_bytes(line)
                except PayloadValidationError as error:
                    yield {"__payload_palette_line_error__": line_number, "errors": _issues(error)}

    return normalize_batch(rows(), policy, max_records=max_records)


def manifest_schema() -> dict[str, Any]:
    """Return the stable JSON Schema subset for a canonical manifest."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://payload-palette.dev/schema/manifest-1.0.json",
        "type": "object",
        "required": ["schema_version", "fingerprint", "part_count", "inline_bytes", "parts"],
        "properties": {
            "schema_version": {"const": "1.0"},
            "fingerprint": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
            "part_count": {"type": "integer", "minimum": 1},
            "inline_bytes": {"type": "integer", "minimum": 0},
            "parts": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/part"}},
        },
        "$defs": {
            "part": {
                "type": "object",
                "required": ["ordinal", "path", "kind", "source", "fingerprint"],
                "properties": {
                    "ordinal": {"type": "integer", "minimum": 0},
                    "path": {"type": "string", "pattern": r"^\$"},
                    "kind": {"enum": ["text", "image", "audio", "video"]},
                    "source": {"enum": ["text", "inline", "remote"]},
                    "fingerprint": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
                },
                "additionalProperties": True,
            }
        },
    }


__all__ = ["BatchRecord", "BatchReport", "manifest_schema", "normalize_batch", "normalize_jsonl"]
