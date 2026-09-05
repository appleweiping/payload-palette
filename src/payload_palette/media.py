"""Bounded Base64 and data-URL decoding plus lightweight signature checks."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from dataclasses import dataclass

from payload_palette.errors import problem

_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_ASCII_WHITESPACE = str.maketrans("", "", " \t\r\n\f\v")
_ASCII_WHITESPACE_CHARACTERS = frozenset(" \t\r\n\f\v")
_URL_SAFE_TO_STANDARD = str.maketrans("-_", "+/")
_MIN_BASE64_WHITESPACE_ALLOWANCE = 4096

# The supported data-URL grammar contains only a MIME type and one ``base64``
# marker.  A fixed header ceiling prevents a small encoded value from using a
# delimiter-heavy header to allocate millions of parameter strings.
MAX_DATA_URL_HEADER_CHARACTERS = 1024

# Base64 is decoded in fixed blocks of encoded characters.  The value is a
# multiple of four so every block except the last carries no padding, and it
# bounds the intermediate copies a streaming caller has to hold: one block is
# 64 KiB of encoded input and 48 KiB of decoded output.
BASE64_BLOCK_CHARACTERS = 64 * 1024

# The longest prefix :func:`detect_mime_type` inspects.
SIGNATURE_PREFIX_BYTES = 12

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


def decode_data_url(
    value: str, path: str, max_bytes: int, *, allow_url_safe: bool = False
) -> InlineMedia:
    """Decode a base64 data URL with a pre-decode resource bound."""

    mime_type, encoded = parse_data_url(value, path)
    return InlineMedia(
        data=decode_base64(encoded, path, max_bytes, allow_url_safe=allow_url_safe),
        mime_type=mime_type,
    )


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


def _uses_url_safe_alphabet(value: str, path: str, *, allow_url_safe: bool) -> bool:
    """Classify a payload as standard or RFC 4648 section 5 URL-safe Base64.

    The two alphabets differ only in their last two characters, so a payload
    containing both is ambiguous rather than merely unsupported: it cannot have
    been produced by a single conforming encoder.  Detection scans the raw value
    because ASCII whitespace never collides with either alphabet.
    """

    url_safe = "-" in value or "_" in value
    if url_safe and ("+" in value or "/" in value):
        raise problem(
            "mixed_base64_alphabet",
            "Base64 payload mixes the standard '+/' and URL-safe '-_' alphabets",
            path,
        )
    if url_safe and not allow_url_safe:
        raise problem(
            "url_safe_base64_disabled",
            "Base64 payload uses the URL-safe alphabet, which is disabled by policy",
            path,
            "accept RFC 4648 section 5 payloads with --allow-url-safe-base64",
        )
    return url_safe


def _bound_base64_size(value: str, path: str, max_bytes: int) -> int:
    """Reject impossible encoded and whitespace sizes before any copy is made."""

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
    return encoded_characters


def _base64_padding(value: str, encoded_characters: int, path: str) -> tuple[int, int]:
    """Return the unpadded character count and the canonical padding length.

    Padding is counted from the end so the caller can plan the whole decode
    without first building a whitespace-free copy of the payload.
    """

    padding = 0
    for character in reversed(value):
        if character in _ASCII_WHITESPACE_CHARACTERS:
            continue
        if character != "=":
            break
        padding += 1
    unpadded_characters = encoded_characters - padding
    remainder = unpadded_characters % 4
    if remainder == 1:
        raise problem("invalid_base64", "Base64 payload has an invalid length", path)
    expected_padding = (4 - remainder) % 4
    if padding not in {0, expected_padding}:
        raise problem("invalid_base64", "Base64 payload has non-canonical padding", path)
    return unpadded_characters, expected_padding


def decode_base64(
    value: str,
    path: str,
    max_bytes: int,
    *,
    allow_url_safe: bool = False,
    sink: Callable[[bytes], object] | None = None,
) -> bytes:
    """Strictly decode padded or unpadded Base64 within a byte limit.

    Only the standard alphabet is accepted unless ``allow_url_safe`` opts into
    RFC 4648 section 5. Either alphabet then decodes under the identical length,
    padding, and resource rules; a payload may not combine them.

    Decoding always advances in ``BASE64_BLOCK_CHARACTERS`` blocks, and every
    limit is enforced against the running decoded total. Without ``sink`` the
    blocks are joined and returned, which holds the whole payload in memory.
    With ``sink`` each block is handed over as soon as it is decoded and nothing
    is retained, so peak memory follows the block size rather than the payload
    size; the returned value is then empty.
    """

    encoded_characters = _bound_base64_size(value, path, max_bytes)
    url_safe = _uses_url_safe_alphabet(value, path, allow_url_safe=allow_url_safe)
    if not encoded_characters:
        raise problem("empty_base64", "Base64 payload cannot be empty", path)
    unpadded_characters, expected_padding = _base64_padding(value, encoded_characters, path)
    collected = bytearray()
    consume: Callable[[bytes], object] = collected.extend if sink is None else sink
    decoded_bytes = 0

    def emit(block: str) -> None:
        nonlocal decoded_bytes
        candidate = block.translate(_URL_SAFE_TO_STANDARD) if url_safe else block
        try:
            decoded = base64.b64decode(candidate, validate=True)
        except (binascii.Error, ValueError) as exc:
            alphabet = "URL-safe" if url_safe else "standard"
            raise problem(
                "invalid_base64", f"payload is not valid {alphabet} Base64", path
            ) from exc
        decoded_bytes += len(decoded)
        if decoded_bytes > max_bytes:
            raise problem(
                "part_too_large",
                f"decoded payload is {decoded_bytes} bytes; limit is {max_bytes} bytes",
                path,
            )
        if base64.b64encode(decoded).decode() != candidate:
            raise problem("invalid_base64", "Base64 payload has non-zero padding bits", path)
        consume(decoded)

    pending = ""
    remaining = unpadded_characters
    for start in range(0, len(value), BASE64_BLOCK_CHARACTERS):
        window = value[start : start + BASE64_BLOCK_CHARACTERS].translate(_ASCII_WHITESPACE)
        pending += window[:remaining]
        remaining = max(remaining - len(window), 0)
        while len(pending) >= BASE64_BLOCK_CHARACTERS:
            emit(pending[:BASE64_BLOCK_CHARACTERS])
            pending = pending[BASE64_BLOCK_CHARACTERS:]
    if pending or expected_padding:
        emit(pending + ("=" * expected_padding))
    return bytes(collected)


def detect_mime_type(data: bytes) -> str | None:
    """Recognize common media signatures without claiming full file validation.

    At most ``SIGNATURE_PREFIX_BYTES`` leading bytes are inspected, so a caller
    that streams a payload can keep only that prefix.
    """

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
