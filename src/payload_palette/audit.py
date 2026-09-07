"""Redacted audit receipts for normalized payloads.

Manifests already remove inline bytes, but a caller may intentionally retain a
remote URL query for downstream fetching.  Audit logs should have a stricter
privacy boundary: they retain only stable identity and sizing facts, never
text content, inline data, or URL query/fragment material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from payload_palette.models import Manifest, NormalizedPart


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    """Privacy-preserving summary suitable for structured logs."""

    fingerprint: str
    part_count: int
    inline_bytes: int
    parts: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "kind": "payload-palette-audit",
            "fingerprint": self.fingerprint,
            "part_count": self.part_count,
            "inline_bytes": self.inline_bytes,
            "parts": [dict(part) for part in self.parts],
        }


def audit_manifest(manifest: Manifest) -> AuditReceipt:
    """Create a deterministic receipt without retaining user content."""

    if not isinstance(manifest, Manifest):
        raise TypeError("manifest must be a Manifest instance")
    parts = tuple(_audit_part(part) for part in manifest.parts)
    return AuditReceipt(manifest.fingerprint, manifest.part_count, manifest.inline_bytes, parts)


def _audit_part(part: NormalizedPart) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ordinal": part.ordinal,
        "path": part.path,
        "kind": part.kind,
        "source": part.source,
        "fingerprint": part.fingerprint,
        "byte_length": part.byte_length,
        "character_count": part.character_count,
        "mime_type": part.mime_type,
    }
    if part.locator is not None:
        result["locator"] = _redact_locator(part.locator)
    if part.text is not None:
        result["text_present"] = True
    return {key: value for key, value in result.items() if value is not None}


def _redact_locator(locator: str) -> str:
    try:
        parsed = urlsplit(locator)
    except ValueError:
        return "[invalid-locator]"
    if not parsed.scheme or not parsed.netloc:
        return locator
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


__all__ = ["AuditReceipt", "audit_manifest"]
