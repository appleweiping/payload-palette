"""Immutable input and output models used by the normalization pipeline."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice
from types import MappingProxyType
from typing import Any, Literal

from payload_palette.errors import PayloadValidationError, ValidationIssue
from payload_palette.media import Base64Summary, parse_data_url

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
MAX_POLICY_PARTS = 1_000_000
MAX_PART_ATTRIBUTES = 64
_SHA256_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MIME_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z")


def _validate_unicode_scalar(value: str, name: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{name} must not contain isolated Unicode surrogate code points")


def _freeze_attributes(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("attributes must be a mapping")
    copied: dict[str, str] = {}
    for index, (key, item) in enumerate(islice(value.items(), MAX_PART_ATTRIBUTES + 1)):
        if index == MAX_PART_ATTRIBUTES:
            raise ValueError(f"attributes may contain at most {MAX_PART_ATTRIBUTES} entries")
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError("attribute names and values must be strings")
        _validate_unicode_scalar(key, "attribute name")
        _validate_unicode_scalar(item, f"attribute {key!r}")
        if key in copied:
            raise ValueError("attributes must not contain duplicate names")
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


def _validate_fingerprint(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    _validate_unicode_scalar(value, name)
    if not _SHA256_FINGERPRINT.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 fingerprint")
    return value


def _parts_fingerprint(parts: tuple[NormalizedPart, ...]) -> str:
    canonical = json.dumps(
        [part._to_dict_unchecked() for part in parts],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


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

    def __post_init__(self) -> None:
        for name in ("code", "message"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
            _validate_unicode_scalar(value, name)
        _validate_optional_text(self.hint, "hint")

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
        _validate_count(self.character_count, "character_count")
        if self.character_count <= LARGE_VALUE_PREFIX_CHARACTERS:
            raise ValueError(
                f"character_count must be greater than {LARGE_VALUE_PREFIX_CHARACTERS}"
            )
        if not isinstance(self.prefix, str) or len(self.prefix) != LARGE_VALUE_PREFIX_CHARACTERS:
            raise ValueError(
                f"prefix must contain exactly {LARGE_VALUE_PREFIX_CHARACTERS} characters"
            )
        _validate_unicode_scalar(self.prefix, "prefix")
        if self.data_url_header is not None:
            _validate_optional_text(self.data_url_header, "data_url_header")
            if (
                not self.data_url_header.lower().startswith("data:")
                or not self.data_url_header.endswith(",")
                or not self.prefix.startswith(self.data_url_header)
            ):
                raise ValueError("data_url_header must be the leading data URL header in prefix")
            # The retained header is a complete data URL with an empty body, so
            # the shared parser can enforce the same grammar and 1,024-character
            # ceiling used by buffered and streamed normalization.
            parse_data_url(self.data_url_header, "data_url_header")
        if (self.media is None) == (self.media_issue is None):
            raise ValueError("a LargeValue carries either a media summary or its refusal")
        if self.media is not None and not isinstance(self.media, Base64Summary):
            raise ValueError("media must be a Base64Summary or None")
        if self.media_issue is not None and not isinstance(self.media_issue, DeferredIssue):
            raise ValueError("media_issue must be a DeferredIssue or None")
        if self.media is not None:
            object.__setattr__(
                self,
                "media",
                Base64Summary(
                    byte_length=self.media.byte_length,
                    digest=self.media.digest,
                    signature_prefix=self.media.signature_prefix,
                ),
            )
        if self.media_issue is not None:
            object.__setattr__(
                self,
                "media_issue",
                DeferredIssue(
                    code=self.media_issue.code,
                    message=self.media_issue.message,
                    hint=self.media_issue.hint,
                ),
            )

    def _validated_snapshot(self) -> LargeValue:
        return LargeValue(
            character_count=self.character_count,
            prefix=self.prefix,
            data_url_header=self.data_url_header,
            media=self.media,
            media_issue=self.media_issue,
        )


def value_length(value: str | LargeValue) -> int:
    """Return the character count of a materialized or streamed string."""

    return len(value) if isinstance(value, str) else value._validated_snapshot().character_count


def value_prefix(value: str | LargeValue, count: int) -> str:
    """Return the leading characters of a materialized or streamed string.

    ``count`` may not exceed what a streamed value retains, so every probe the
    pipeline makes reads real characters rather than a silently short answer.
    """

    _validate_count(count, "count")
    if count > LARGE_VALUE_PREFIX_CHARACTERS:
        raise ValueError(f"at most {LARGE_VALUE_PREFIX_CHARACTERS} leading characters are retained")
    return value[:count] if isinstance(value, str) else value._validated_snapshot().prefix[:count]


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
        if isinstance(self.value, LargeValue):
            object.__setattr__(self, "value", self.value._validated_snapshot())
        else:
            _validate_unicode_scalar(self.value, "value")
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
        if not isinstance(self.path, str) or not self.path.startswith("$"):
            raise ValueError("path must be a non-empty JSON path beginning with '$'")
        _validate_unicode_scalar(self.path, "path")
        _validate_fingerprint(self.fingerprint, "fingerprint")
        _validate_optional_count(self.byte_length, "byte_length")
        _validate_optional_count(self.character_count, "character_count")
        for name in ("mime_type", "text", "locator"):
            _validate_optional_text(getattr(self, name), name)
        if self.mime_type is not None and _MIME_TYPE.fullmatch(self.mime_type) is None:
            raise ValueError("mime_type must be a normalized lowercase MIME type")
        object.__setattr__(self, "attributes", _freeze_attributes(self.attributes))
        self._validate_shape()

    def _validate_shape(self) -> None:
        if self.source == "text":
            if self.kind != "text":
                raise ValueError("text source requires kind='text'")
            if self.text is None:
                raise ValueError("text source requires text")
            encoded_length = len(self.text.encode("utf-8"))
            if self.byte_length != encoded_length:
                raise ValueError("text byte_length must equal its UTF-8 byte length")
            if self.character_count != len(self.text):
                raise ValueError("text character_count must equal len(text)")
            if self.mime_type != "text/plain":
                raise ValueError("text source requires mime_type='text/plain'")
            if self.locator is not None:
                raise ValueError("text source must not carry a locator")
            expected_fingerprint = f"sha256:{hashlib.sha256(self.text.encode('utf-8')).hexdigest()}"
            if self.fingerprint != expected_fingerprint:
                raise ValueError("text fingerprint must match its UTF-8 bytes")
            return
        if self.kind == "text":
            raise ValueError("text kind requires source='text'")
        if self.text is not None or self.character_count is not None:
            raise ValueError("media parts must not carry text or character_count")
        if self.source == "inline":
            if self.byte_length is None or self.byte_length < 1:
                raise ValueError("inline media requires a positive byte_length")
            if not self.mime_type:
                raise ValueError("inline media requires a MIME type")
            if self.locator != "inline":
                raise ValueError("inline media requires locator='inline'")
            return
        if self.byte_length is not None:
            raise ValueError("remote media must not claim a byte_length")
        if not self.locator:
            raise ValueError("remote media requires a non-empty locator")

    def _validated_snapshot(self) -> NormalizedPart:
        return NormalizedPart(
            ordinal=self.ordinal,
            path=self.path,
            kind=self.kind,
            source=self.source,
            fingerprint=self.fingerprint,
            byte_length=self.byte_length,
            character_count=self.character_count,
            mime_type=self.mime_type,
            text=self.text,
            locator=self.locator,
            attributes=self.attributes,
        )

    def _to_dict_unchecked(self) -> dict[str, Any]:
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

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic dictionary after revalidating the frozen shell."""

        return self._validated_snapshot()._to_dict_unchecked()


@dataclass(frozen=True, slots=True)
class Manifest:
    """Canonical description of a validated multimodal request."""

    parts: tuple[NormalizedPart, ...]
    fingerprint: str
    inline_bytes: int
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        _validate_count(self.inline_bytes, "inline_bytes")
        _validate_fingerprint(self.fingerprint, "fingerprint")
        if self.schema_version != "1.0":
            raise ValueError("schema_version must be '1.0'")
        if isinstance(self.parts, str | bytes) or not isinstance(self.parts, Sequence):
            raise ValueError("parts must be a sequence of NormalizedPart values")
        parts: list[NormalizedPart] = []
        for index, part in enumerate(islice(iter(self.parts), MAX_POLICY_PARTS + 1)):
            if index == MAX_POLICY_PARTS:
                raise ValueError(f"parts may contain at most {MAX_POLICY_PARTS} entries")
            if not isinstance(part, NormalizedPart):
                raise ValueError(f"parts[{index}] must be a NormalizedPart")
            parts.append(part._validated_snapshot())
        if not parts:
            raise ValueError("parts must contain at least one NormalizedPart")
        frozen_parts = tuple(parts)
        if tuple(part.ordinal for part in frozen_parts) != tuple(range(len(frozen_parts))):
            raise ValueError("part ordinals must be contiguous and match tuple order")
        expected_inline_bytes = sum(
            part.byte_length or 0 for part in frozen_parts if part.source in {"text", "inline"}
        )
        if self.inline_bytes != expected_inline_bytes:
            raise ValueError("inline_bytes must equal the total bytes of text and inline parts")
        expected_fingerprint = _parts_fingerprint(frozen_parts)
        if self.fingerprint != expected_fingerprint:
            raise ValueError("fingerprint must match the canonical ordered parts")
        object.__setattr__(self, "parts", frozen_parts)

    @property
    def part_count(self) -> int:
        """Number of parts in original request order."""

        return len(self.parts)

    def _to_dict_unchecked(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fingerprint": self.fingerprint,
            "part_count": self.part_count,
            "inline_bytes": self.inline_bytes,
            "parts": [part._to_dict_unchecked() for part in self.parts],
        }

    def to_dict(self) -> dict[str, Any]:
        """Return canonical JSON after revalidating every nested part."""

        snapshot = Manifest(
            parts=self.parts,
            fingerprint=self.fingerprint,
            inline_bytes=self.inline_bytes,
            schema_version=self.schema_version,
        )
        return snapshot._to_dict_unchecked()
