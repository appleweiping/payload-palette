"""High-level validation and canonical manifest construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from payload_palette.errors import PayloadValidationError, ValidationIssue, problem
from payload_palette.media import (
    SIGNATURE_PREFIX_BYTES,
    decode_base64,
    detect_mime_type,
    normalize_mime_type,
    parse_data_url,
)
from payload_palette.models import (
    LargeValue,
    Manifest,
    NormalizedPart,
    PartSpec,
    value_length,
    value_prefix,
)
from payload_palette.parser import parse_document
from payload_palette.policy import MAX_REMOTE_URL_CHARACTERS, NormalizationPolicy

_FINGERPRINT_PREFIX = "sha256:"


def _sha256(value: bytes) -> str:
    return f"{_FINGERPRINT_PREFIX}{hashlib.sha256(value).hexdigest()}"


@dataclass(frozen=True, slots=True)
class _InlineSummary:
    """Everything a manifest records about inline media, without its bytes."""

    byte_length: int
    fingerprint: str
    signature_prefix: bytes


def _summarize_inline(
    encoded: str, path: str, max_bytes: int, policy: NormalizationPolicy
) -> _InlineSummary:
    """Decode inline Base64 in bounded blocks without retaining the payload.

    A manifest needs only the decoded size, its digest, and the leading
    signature bytes, so each block is folded into those three values and then
    released instead of being accumulated into one large buffer.
    """

    digest = hashlib.sha256()
    prefix = bytearray()
    byte_length = 0

    def absorb(block: bytes) -> None:
        nonlocal byte_length
        digest.update(block)
        byte_length += len(block)
        if len(prefix) < SIGNATURE_PREFIX_BYTES:
            prefix.extend(block[: SIGNATURE_PREFIX_BYTES - len(prefix)])

    decode_base64(
        encoded,
        path,
        max_bytes,
        allow_url_safe=policy.allow_url_safe_base64,
        sink=absorb,
    )
    return _InlineSummary(
        byte_length=byte_length,
        fingerprint=f"{_FINGERPRINT_PREFIX}{digest.hexdigest()}",
        signature_prefix=bytes(prefix),
    )


def _split_data_url(value: str | LargeValue, path: str) -> tuple[str, str | LargeValue]:
    """Return a data URL's MIME type and its still-unmaterialized payload.

    The header of a streamed data URL is inside the retained prefix, so it goes
    through exactly the grammar a buffered header does; only the payload after
    the comma stays a measurement.
    """

    if isinstance(value, str):
        return parse_data_url(value, path)
    header = value.data_url_header
    if header is None:
        raise problem("invalid_data_url", "data URL is missing its comma separator", path)
    mime_type, _empty = parse_data_url(header, path)
    return mime_type, value


def _inline_summary(
    value: str | LargeValue, path: str, max_bytes: int, policy: NormalizationPolicy
) -> _InlineSummary:
    """Summarize inline media whether it arrived buffered or streamed.

    A streamed value was already reduced to these three numbers while its
    characters went past, so there is nothing left to decode; the refusal the
    stream recorded is raised here instead, against the content path, because
    only now is it certain the value was meant to be media at all.
    """

    if isinstance(value, str):
        return _summarize_inline(value, path, max_bytes, policy)
    if value.media_issue is not None:
        raise value.media_issue.raised_at(path)
    media = value.media
    if media is None:  # pragma: no cover - LargeValue guarantees one or the other
        raise problem("invalid_base64", "inline media could not be summarized", path)
    return _InlineSummary(
        byte_length=media.byte_length,
        fingerprint=f"{_FINGERPRINT_PREFIX}{media.digest}",
        signature_prefix=media.signature_prefix,
    )


def _normalize_text(part: PartSpec, policy: NormalizationPolicy) -> NormalizedPart:
    policy.check_mime("text", "text/plain", part.value_path)
    value = part.value
    # A streamed value was left unmaterialized precisely because it is longer
    # than any acceptable text part, so the same limit refuses it -- and reports
    # the true length, which the stream counted without keeping the characters.
    if not isinstance(value, str) or len(value) > policy.max_text_characters:
        characters = value_length(value)
        raise problem(
            "text_too_long",
            f"text contains {characters} characters; limit is {policy.max_text_characters}",
            part.value_path,
        )
    encoded = value.encode("utf-8")
    policy.check_size("text", len(encoded), part.value_path)
    return NormalizedPart(
        ordinal=part.ordinal,
        path=part.path,
        kind="text",
        source="text",
        fingerprint=_sha256(encoded),
        byte_length=len(encoded),
        character_count=len(value),
        mime_type="text/plain",
        text=value,
    )


def _normalize_inline(part: PartSpec, policy: NormalizationPolicy) -> NormalizedPart:
    maximum = policy.max_bytes_by_kind[part.kind]
    declared = (
        normalize_mime_type(part.declared_mime_type, part.value_path)
        if part.declared_mime_type is not None
        else None
    )
    if value_prefix(part.value, 5).lower() == "data:":
        mime_type, encoded = _split_data_url(part.value, part.value_path)
        if declared is not None and declared != mime_type:
            raise problem(
                "mime_conflict",
                f"declared MIME {declared!r} conflicts with data URL MIME {mime_type!r}",
                part.value_path,
            )
        policy.check_mime(part.kind, mime_type, part.value_path)
        summary = _inline_summary(encoded, part.value_path, maximum, policy)
    elif declared is not None:
        mime_type = declared
        policy.check_mime(part.kind, mime_type, part.value_path)
        summary = _inline_summary(part.value, part.value_path, maximum, policy)
    else:
        # An envelope such as Ollama's carries inline images with no MIME
        # declaration at all.  The signature is then the only statement about
        # the media, so it becomes the MIME type instead of something to
        # compare a declaration against.  Decoding stays bounded by the same
        # per-kind ceiling as a declared part, so this only moves the MIME
        # allowlist check after the decode it would otherwise have preceded.
        summary = _inline_summary(part.value, part.value_path, maximum, policy)
        recognized = detect_mime_type(summary.signature_prefix)
        if recognized is None:
            raise problem(
                "undeclared_media_type",
                "inline media has no declared MIME type and no recognizable signature",
                part.value_path,
                "this envelope declares no MIME type, so the leading bytes must "
                "match a supported container signature",
            )
        mime_type = recognized
        policy.check_mime(part.kind, mime_type, part.value_path)
    policy.check_size(part.kind, summary.byte_length, part.value_path)
    detected = detect_mime_type(summary.signature_prefix)
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
        fingerprint=summary.fingerprint,
        byte_length=summary.byte_length,
        mime_type=mime_type,
        locator="inline",
        attributes=part.attributes,
    )


def _normalize_remote(part: PartSpec, policy: NormalizationPolicy) -> NormalizedPart:
    value = part.value
    if not isinstance(value, str):
        # The streaming threshold is never below the URL ceiling, so a value the
        # stream declined to materialize is already past it.
        raise problem(
            "url_too_long",
            f"remote URL exceeds the {MAX_REMOTE_URL_CHARACTERS}-character limit",
            part.value_path,
        )
    display_url, canonical_url = policy.remote.validate(value, part.value_path)
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
    specs = parse_document(document, active_policy.max_parts, envelope=active_policy.envelope)
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
