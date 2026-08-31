"""Bounded Base64 and data-URL decoding plus lightweight signature checks."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from payload_palette.errors import problem

_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_ASCII_WHITESPACE = str.maketrans("", "", " \t\r\n\f\v")
_ASCII_WHITESPACE_CHARACTERS = frozenset(" \t\r\n\f\v")
_MIN_BASE64_WHITESPACE_ALLOWANCE = 4096

# The supported data-URL grammar contains only a MIME type and one ``base64``
# marker.  A fixed header ceiling prevents a small encoded value from using a
# delimiter-heavy header to allocate millions of parameter strings.
MAX_DATA_URL_HEADER_CHARACTERS = 1024

FORMAT_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "wav": "audio/wav",
    "wave": "audio/wav",
    "mp3": "audio/mpeg",
    "mpeg": "audio/mpeg",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
    "m4a": "audio/mp4",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
}


@dataclass(frozen=True, slots=True)
class InlineMedia:
    """Decoded bytes and normalized MIME metadata."""

    data: bytes
    mime_type: str


def normalize_mime_type(value: str, path: str) -> str:
    """Lowercase a MIME type and discard parameters after validating syntax."""

    normalized = value.split(";", 1)[0].strip().lower()
    if not _MIME.fullmatch(normalized):
        raise problem("invalid_mime", f"invalid MIME type {value!r}", path)
    return normalized


def mime_from_format(value: str | None, kind: str | None = None) -> str | None:
    """Map a familiar file-format label to a MIME type when known."""

    if value is None:
        return None
    normalized = value.strip().lower().lstrip(".")
    if normalized == "mp4" and kind == "audio":
        return "audio/mp4"
    if normalized == "webm" and kind == "audio":
        return "audio/webm"
    return FORMAT_MIME_TYPES.get(normalized)


def decode_data_url(value: str, path: str, max_bytes: int) -> InlineMedia:
    """Decode a base64 data URL with a pre-decode resource bound."""

    mime_type, encoded = parse_data_url(value, path)
    return InlineMedia(data=decode_base64(encoded, path, max_bytes), mime_type=mime_type)


def parse_data_url(value: str, path: str) -> tuple[str, str]:
    """Return a validated MIME type and encoded payload without decoding bytes.

    Header size and grammar are rejected before slicing the potentially large
    encoded portion.  Callers can therefore enforce MIME policy and declaration
    consistency before paying the Base64 decoding cost.
    """

    if value[:5].lower() != "data:":
        raise problem("invalid_data_url", "value is not a data URL", path)
    comma = value.find(",", 5)
    if comma < 0:
        raise problem("invalid_data_url", "data URL is missing its comma separator", path)
    header_length = comma - 5
    if header_length > MAX_DATA_URL_HEADER_CHARACTERS:
        raise problem(
            "data_url_header_too_long",
            f"data URL header exceeds the {MAX_DATA_URL_HEADER_CHARACTERS}-character limit",
            path,
        )
    header = value[5:comma]
    mime_value, separator, parameters = header.partition(";")
    mime_type = normalize_mime_type(mime_value or "text/plain", path)
    if not separator or not parameters:
        raise problem(
            "data_url_encoding",
            "binary data URLs must use the ;base64 encoding marker",
            path,
        )
    marker, extra_separator, _extra = parameters.partition(";")
    if marker.lower() != "base64" or extra_separator:
        raise problem(
            "data_url_parameter",
            "the data URL must contain exactly one ;base64 marker",
            path,
        )
    return mime_type, value[comma + 1 :]


def decode_base64(value: str, path: str, max_bytes: int) -> bytes:
    """Strictly decode padded or unpadded standard Base64 within a byte limit."""

    maximum_characters = 4 * ((max_bytes + 2) // 3)
    whitespace_allowance = max(_MIN_BASE64_WHITESPACE_ALLOWANCE, maximum_characters // 8)
    if len(value) > maximum_characters + whitespace_allowance:
        raise problem(
            "part_too_large",
            "Base64 representation has excessive encoded or whitespace characters",
            path,
        )
    encoded_characters = sum(character not in _ASCII_WHITESPACE_CHARACTERS for character in value)
    if encoded_characters > maximum_characters:
        raise problem(
            "part_too_large",
            f"encoded payload can exceed the {max_bytes}-byte decoded limit",
            path,
        )
    compact = value.translate(_ASCII_WHITESPACE)
    if not compact:
        raise problem("empty_base64", "Base64 payload cannot be empty", path)
    unpadded = compact.rstrip("=")
    padding = len(compact) - len(unpadded)
    remainder = len(unpadded) % 4
    if remainder == 1:
        raise problem("invalid_base64", "Base64 payload has an invalid length", path)
    expected_padding = (4 - remainder) % 4
    if padding not in {0, expected_padding}:
        raise problem("invalid_base64", "Base64 payload has non-canonical padding", path)
    padded = unpadded + ("=" * expected_padding)
    try:
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise problem("invalid_base64", "payload is not valid standard Base64", path) from exc
    if len(decoded) > max_bytes:
        raise problem(
            "part_too_large",
            f"decoded payload is {len(decoded)} bytes; limit is {max_bytes} bytes",
            path,
        )
    if base64.b64encode(decoded).decode().rstrip("=") != unpadded:
        raise problem("invalid_base64", "Base64 payload has non-zero padding bits", path)
    return decoded


def detect_mime_type(data: bytes) -> str | None:
    """Recognize common media signatures without claiming full file validation."""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        return "audio/mpeg"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/quicktime" if data[8:12] == b"qt  " else "video/mp4"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    return None
