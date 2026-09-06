"""Immutable input and output models used by the normalization pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from payload_palette.errors import PayloadValidationError, ValidationIssue
from payload_palette.media import Base64Summary

# How much of an over-long string a streamed value retains.  A ``data:`` URL
# header is bounded well below this, so the comma ending it is always inside
# the prefix and every shorter probe reads real characters.
LARGE_VALUE_PREFIX_CHARACTERS = 2048

PartKind = Literal["text", "image", "audio", "video"]
SourceKind = Literal["text", "inline", "remote"]
EnvelopeName = Literal["default", "anthropic", "gemini", "ollama"]

# Every request envelope the parser can be asked to read.  Adapters are opt-in:
# "default" keeps the original contract and no vendor shape is ever detected
# from the document, because guessing would weaken the deliberate rejection of
# an ambiguous envelope.
ENVELOPE_NAMES: tuple[EnvelopeName, ...] = ("default", "anthropic", "gemini", "ollama")

# CPython permits configuring its integer-to-decimal conversion limit as low as
# 640 digits.  Keeping public model integers within that boundary means their
# documented JSON representation remains serializable even in a maximally
# hardened process.
MAX_MODEL_INTEGER_DIGITS = 640
_MAX_MODEL_INTEGER = (10**MAX_MODEL_INTEGER_DIGITS) - 1


def _validate_unicode_scalar(value: str, name: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{name} must not contain isolated Unicode surrogate code points")


def _freeze_attributes(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("attributes must be a mapping")
    copied: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("attribute names and values must be strings")
        _validate_unicode_scalar(key, "attribute name")
        _validate_unicode_scalar(item, f"attribute {key!r}")
        copied[key] = item
    return MappingProxyType(copied)


def _validate_optional_text(value: str | None, name: str) -> None:
    if value is not None:
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string or None")
        _validate_unicode_scalar(value, name)


def _validate_count(value: object, name: str) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_MODEL_INTEGER:
        raise ValueError(
            f"{name} must be a non-negative integer of at most "
            f"{MAX_MODEL_INTEGER_DIGITS} decimal digits"
        )


def _validate_optional_count(value: int | None, name: str) -> None:
    if value is not None:
        _validate_count(value, name)


@dataclass(frozen=True, slots=True)
class DeferredIssue:
    """A refusal discovered while streaming, re-raised against the real path.

    A long string is summarized before anything knows what it is.  Refusing a
    malformed Base64 payload at that moment would report it at the JSON path
    rather than at the content path the rest of the pipeline uses, and would
    refuse a *text* part for a fault that only matters to media.  The refusal
    therefore travels with the value and is raised by whoever decides the value
    is media.
    """

    code: str
    message: str
    hint: str | None = None

    def raised_at(self, path: str) -> PayloadValidationError:
        return PayloadValidationError([ValidationIssue(self.code, self.message, path, self.hint)])


@dataclass(frozen=True, slots=True)
class LargeValue:
    """A string kept as measurements instead of characters.

    ``prefix`` holds the first :data:`LARGE_VALUE_PREFIX_CHARACTERS` characters,
    ``character_count`` the true length, and ``media`` the Base64 summary
    computed while the value streamed past -- or ``media_issue`` if that summary
    could not be produced.  Exactly one of the two is set.
    """

    character_count: int
    prefix: str
    data_url_header: str | None = None
    media: Base64Summary | None = None
    media_issue: DeferredIssue | None = None

    def __post_init__(self) -> None:
        if (self.media is None) == (self.media_issue is None):
            raise ValueError("a LargeValue carries either a media summary or its refusal")


def value_length(value: str | LargeValue) -> int:
    """Return the character count of a materialized or streamed string."""

    return len(value) if isinstance(value, str) else value.character_count


def value_prefix(value: str | LargeValue, count: int) -> str:
    """Return the leading characters of a materialized or streamed string.

    ``count`` may not exceed what a streamed value retains, so every probe the
    pipeline makes reads real characters rather than a silently short answer.
    """

    if count > LARGE_VALUE_PREFIX_CHARACTERS:
        raise ValueError(f"at most {LARGE_VALUE_PREFIX_CHARACTERS} leading characters are retained")
    return value[:count] if isinstance(value, str) else value.prefix[:count]


@dataclass(frozen=True, slots=True)
class PartSpec:
    """A parsed content part before media decoding and policy checks."""

    ordinal: int
    path: str
    value_path: str
    kind: PartKind
    value: str | LargeValue
    source_hint: SourceKind
    declared_mime_type: str | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_count(self.ordinal, "ordinal")
        if not isinstance(self.value, (str, LargeValue)):
            raise ValueError("value must be a string or a streamed LargeValue")
        for name in ("path", "value_path"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            _validate_unicode_scalar(value, name)
        if self.kind not in {"text", "image", "audio", "video"}:
            raise ValueError(f"unsupported part kind: {self.kind!r}")
        if self.source_hint not in {"text", "inline", "remote"}:
            raise ValueError(f"unsupported source hint: {self.source_hint!r}")
        _validate_optional_text(self.declared_mime_type, "declared_mime_type")
        object.__setattr__(self, "attributes", _freeze_attributes(self.attributes))


@dataclass(frozen=True, slots=True)
class NormalizedPart:
    """A safe manifest entry that never embeds binary payload bytes."""

    ordinal: int
    path: str
    kind: PartKind
    source: SourceKind
    fingerprint: str
    byte_length: int | None = None
    character_count: int | None = None
    mime_type: str | None = None
    text: str | None = None
    locator: str | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_count(self.ordinal, "ordinal")
        if self.kind not in {"text", "image", "audio", "video"}:
            raise ValueError(f"unsupported normalized part kind: {self.kind!r}")
        if self.source not in {"text", "inline", "remote"}:
            raise ValueError(f"unsupported normalized part source: {self.source!r}")
        for name in ("path", "fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            _validate_unicode_scalar(value, name)
        _validate_optional_count(self.byte_length, "byte_length")
        _validate_optional_count(self.character_count, "character_count")
        for name in ("mime_type", "text", "locator"):
            _validate_optional_text(getattr(self, name), name)
        object.__setattr__(self, "attributes", _freeze_attributes(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic dictionary with absent optional values omitted."""

        values: dict[str, Any] = {
            "ordinal": self.ordinal,
            "path": self.path,
            "kind": self.kind,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "byte_length": self.byte_length,
            "character_count": self.character_count,
            "mime_type": self.mime_type,
            "text": self.text,
            "locator": self.locator,
        }
        if self.attributes:
            values["attributes"] = dict(sorted(self.attributes.items()))
        return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True, slots=True)
class Manifest:
    """Canonical description of a validated multimodal request."""

    parts: tuple[NormalizedPart, ...]
    fingerprint: str
    inline_bytes: int
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        parts = tuple(self.parts)
        if any(not isinstance(part, NormalizedPart) for part in parts):
            raise ValueError("parts must contain only NormalizedPart values")
        _validate_count(self.inline_bytes, "inline_bytes")
        for name in ("fingerprint", "schema_version"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            _validate_unicode_scalar(value, name)
        object.__setattr__(self, "parts", parts)

    @property
    def part_count(self) -> int:
        """Number of parts in original request order."""

        return len(self.parts)

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible manifest."""

        return {
            "schema_version": self.schema_version,
            "fingerprint": self.fingerprint,
            "part_count": self.part_count,
            "inline_bytes": self.inline_bytes,
            "parts": [part.to_dict() for part in self.parts],
        }
