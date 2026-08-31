"""High-level validation and canonical manifest construction."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from payload_palette.errors import PayloadValidationError, ValidationIssue, problem
from payload_palette.media import (
    decode_base64,
    detect_mime_type,
    normalize_mime_type,
    parse_data_url,
)
from payload_palette.models import Manifest, NormalizedPart, PartSpec
from payload_palette.parser import parse_document
from payload_palette.policy import NormalizationPolicy


def _sha256(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _normalize_text(part: PartSpec, policy: NormalizationPolicy) -> NormalizedPart:
    policy.check_mime("text", "text/plain", part.value_path)
    if len(part.value) > policy.max_text_characters:
        raise problem(
            "text_too_long",
            f"text contains {len(part.value)} characters; limit is {policy.max_text_characters}",
            part.value_path,
        )
    encoded = part.value.encode("utf-8")
    policy.check_size("text", len(encoded), part.value_path)
    return NormalizedPart(
        ordinal=part.ordinal,
        path=part.path,
        kind="text",
        source="text",
        fingerprint=_sha256(encoded),
        byte_length=len(encoded),
        character_count=len(part.value),
        mime_type="text/plain",
        text=part.value,
    )


def _normalize_inline(part: PartSpec, policy: NormalizationPolicy) -> NormalizedPart:
    maximum = policy.max_bytes_by_kind[part.kind]
    declared = (
        normalize_mime_type(part.declared_mime_type, part.value_path)
        if part.declared_mime_type is not None
        else None
    )
    if part.value[:5].lower() == "data:":
        mime_type, encoded = parse_data_url(part.value, part.value_path)
        if declared is not None and declared != mime_type:
            raise problem(
                "mime_conflict",
                f"declared MIME {declared!r} conflicts with data URL MIME {mime_type!r}",
                part.value_path,
            )
        policy.check_mime(part.kind, mime_type, part.value_path)
        data = decode_base64(encoded, part.value_path, maximum)
    else:
        if declared is None:  # parser normally prevents this; retained as an internal invariant.
            raise problem("missing_mime", "inline media requires a MIME type", part.value_path)
        mime_type = declared
        policy.check_mime(part.kind, mime_type, part.value_path)
        data = decode_base64(part.value, part.value_path, maximum)
    policy.check_size(part.kind, len(data), part.value_path)
    detected = detect_mime_type(data)
    if policy.verify_known_signatures and detected is not None and detected != mime_type:
        compatible_container = (detected, mime_type) in {
            ("video/mp4", "audio/mp4"),
            ("video/webm", "audio/webm"),
        }
        if not compatible_container:
            raise problem(
                "signature_mismatch",
                f"content signature looks like {detected!r}, not declared {mime_type!r}",
                part.value_path,
            )
    return NormalizedPart(
        ordinal=part.ordinal,
        path=part.path,
        kind=part.kind,
        source="inline",
        fingerprint=_sha256(data),
        byte_length=len(data),
        mime_type=mime_type,
        locator="inline",
        attributes=part.attributes,
    )


def _normalize_remote(part: PartSpec, policy: NormalizationPolicy) -> NormalizedPart:
    display_url, canonical_url = policy.remote.validate(part.value, part.value_path)
    mime_type = None
    if part.declared_mime_type is not None:
        mime_type = normalize_mime_type(part.declared_mime_type, part.value_path)
        policy.check_mime(part.kind, mime_type, part.value_path)
    return NormalizedPart(
        ordinal=part.ordinal,
        path=part.path,
        kind=part.kind,
        source="remote",
        fingerprint=_sha256(canonical_url.encode("utf-8")),
        mime_type=mime_type,
        locator=display_url,
        attributes=part.attributes,
    )


def normalize(document: Any, policy: NormalizationPolicy | None = None) -> Manifest:
    """Validate a request and return a deterministic, binary-free manifest.

    Independent per-part failures are aggregated. No network requests or file
    reads are performed by this function.
    """

    if policy is not None and not isinstance(policy, NormalizationPolicy):
        raise ValueError("policy must be a NormalizationPolicy or None")
    active_policy = policy if policy is not None else NormalizationPolicy()
    specs = parse_document(document, active_policy.max_parts)
    normalized: list[NormalizedPart] = []
    issues: list[ValidationIssue] = []
    inline_bytes = 0
    for part in specs:
        try:
            if part.kind == "text":
                normalized_part = _normalize_text(part, active_policy)
            elif part.source_hint == "remote":
                normalized_part = _normalize_remote(part, active_policy)
            else:
                normalized_part = _normalize_inline(part, active_policy)
        except PayloadValidationError as exc:
            issues.extend(exc.issues)
            continue
        if normalized_part.source in {"inline", "text"}:
            inline_bytes += normalized_part.byte_length or 0
            if inline_bytes > active_policy.max_total_inline_bytes:
                issues.append(
                    ValidationIssue(
                        "total_too_large",
                        f"inline payload exceeds the "
                        f"{active_policy.max_total_inline_bytes}-byte total limit",
                        "$",
                    )
                )
                break
        normalized.append(normalized_part)
    if issues:
        raise PayloadValidationError(issues)
    canonical_parts = [part.to_dict() for part in normalized]
    canonical = json.dumps(
        canonical_parts,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return Manifest(
        parts=tuple(normalized),
        fingerprint=_sha256(canonical),
        inline_bytes=inline_bytes,
    )


def validate(
    document: Any, policy: NormalizationPolicy | None = None
) -> tuple[ValidationIssue, ...]:
    """Return validation issues instead of raising; an empty tuple means valid."""

    try:
        normalize(document, policy)
    except PayloadValidationError as exc:
        return exc.issues
    return ()
