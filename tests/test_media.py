from __future__ import annotations

import base64

import pytest

from payload_palette.errors import PayloadValidationError
from payload_palette.media import (
    BASE64_BLOCK_CHARACTERS,
    MAX_DATA_URL_HEADER_CHARACTERS,
    SIGNATURE_PREFIX_BYTES,
    Base64Summary,
    decode_base64,
    decode_data_url,
    detect_mime_type,
    mime_from_format,
    normalize_mime_type,
)


def _repeating_bytes(size: int) -> bytes:
    pattern = b"\x89PNG\r\n\x1a\npayload-palette\x00\xfb\xf0"
    return (pattern * (size // len(pattern) + 1))[:size]


@pytest.mark.parametrize(
    ("byte_length", "digest", "signature_prefix", "message"),
    [
        (0, "0" * 64, b"", "byte_length"),
        (True, "0" * 64, b"x", "byte_length"),
        (1, "A" * 64, b"x", "digest"),
        (1, "0" * 63, b"x", "digest"),
        (1, "0" * 64, bytearray(b"x"), "bytes"),
        (2, "0" * 64, b"x", "length"),
        (13, "0" * 64, b"x" * 13, "length"),
    ],
)
def test_base64_summary_rejects_inconsistent_direct_construction(
    byte_length: object,
    digest: object,
    signature_prefix: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        Base64Summary(byte_length, digest, signature_prefix)  # type: ignore[arg-type]


def test_decodes_padded_and_unpadded_base64() -> None:
    encoded = base64.b64encode(b"hello").decode()
    assert decode_base64(encoded, "$", 10) == b"hello"
    assert decode_base64(encoded.rstrip("="), "$", 10) == b"hello"


def test_base64_round_trip_property_for_small_deterministic_payloads() -> None:
    for size in range(1, 65):
        data = bytes((index * 37 + size) % 256 for index in range(size))
        encoded = base64.b64encode(data).decode()
        assert decode_base64(encoded, "$", 64) == data
        assert decode_base64(encoded.rstrip("="), "$", 64) == data


def test_base64_allows_ascii_whitespace() -> None:
    assert decode_base64("aG Vs\nbG8=", "$", 10) == b"hello"


@pytest.mark.parametrize("value", ["A", "%%%", "====", ""])
def test_invalid_base64_rejected(value: str) -> None:
    with pytest.raises(PayloadValidationError):
        decode_base64(value, "$.data", 10)


@pytest.mark.parametrize("value", ["AA=", "AA===", "aGVsbG8==", "AB==", "AAB="])
def test_noncanonical_padding_and_padding_bits_are_rejected(value: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        decode_base64(value, "$.data", 10)
    assert error.value.issues[0].code == "invalid_base64"


def test_encoded_size_is_bounded_before_decode() -> None:
    with pytest.raises(PayloadValidationError) as error:
        decode_base64(base64.b64encode(b"too long").decode(), "$", 2)
    assert error.value.issues[0].code == "part_too_large"


def test_base64_whitespace_amplification_is_bounded() -> None:
    with pytest.raises(PayloadValidationError) as error:
        decode_base64((" " * 4097) + "AA==", "$", 1)
    assert error.value.issues[0].code == "part_too_large"


def test_decoded_size_is_checked_after_decode() -> None:
    encoded = base64.b64encode(b"12345").decode()
    with pytest.raises(PayloadValidationError, match="decoded payload is 5 bytes"):
        decode_base64(encoded, "$", 4)


def test_url_safe_base64_is_rejected_by_default(url_safe_png_base64: str) -> None:
    assert "-" in url_safe_png_base64 and "_" in url_safe_png_base64
    with pytest.raises(PayloadValidationError) as error:
        decode_base64(url_safe_png_base64, "$.data", 64)
    issue = error.value.issues[0]
    assert issue.code == "url_safe_base64_disabled"
    assert issue.path == "$.data"
    assert issue.hint is not None


def test_url_safe_base64_decodes_to_the_same_bytes_when_enabled(
    url_safe_png_base64: str, standard_png_base64: str
) -> None:
    expected = decode_base64(standard_png_base64, "$", 64)
    assert decode_base64(url_safe_png_base64, "$", 64, allow_url_safe=True) == expected
    assert decode_base64(url_safe_png_base64.rstrip("="), "$", 64, allow_url_safe=True) == expected
    assert decode_base64("aG Vs\nbG-_", "$", 64, allow_url_safe=True) == decode_base64(
        "aGVsbG+/", "$", 64
    )


def test_enabling_url_safe_base64_still_accepts_the_standard_alphabet(
    standard_png_base64: str,
) -> None:
    assert decode_base64(standard_png_base64, "$", 64, allow_url_safe=True) == decode_base64(
        standard_png_base64, "$", 64
    )


@pytest.mark.parametrize("allow_url_safe", [False, True])
def test_mixed_base64_alphabets_are_always_rejected(allow_url_safe: bool) -> None:
    with pytest.raises(PayloadValidationError) as error:
        decode_base64("aGVsbG-/", "$.data", 64, allow_url_safe=allow_url_safe)
    assert error.value.issues[0].code == "mixed_base64_alphabet"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("-A=", "non-canonical padding"),
        ("-", "invalid length"),
        ("-B==", "non-zero padding bits"),
        ("-%%A", "not valid URL-safe Base64"),
    ],
)
def test_url_safe_payloads_obey_the_standard_strictness_rules(value: str, message: str) -> None:
    with pytest.raises(PayloadValidationError, match=message) as error:
        decode_base64(value, "$.data", 10, allow_url_safe=True)
    assert error.value.issues[0].code == "invalid_base64"


def test_url_safe_base64_is_bounded_before_decode(url_safe_png_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        decode_base64(url_safe_png_base64, "$", 2, allow_url_safe=True)
    assert error.value.issues[0].code == "part_too_large"


def test_streaming_sink_receives_bounded_ordered_blocks() -> None:
    payload = _repeating_bytes(200_000)
    encoded = base64.b64encode(payload).decode()
    blocks: list[bytes] = []
    assert decode_base64(encoded, "$", len(payload), sink=blocks.append) == b""
    assert len(blocks) > 1
    assert max(len(block) for block in blocks) <= BASE64_BLOCK_CHARACTERS // 4 * 3
    assert b"".join(blocks) == payload
    assert decode_base64(encoded, "$", len(payload)) == payload


def test_streaming_handles_a_block_aligned_payload_without_padding() -> None:
    payload = _repeating_bytes(BASE64_BLOCK_CHARACTERS // 4 * 3)
    encoded = base64.b64encode(payload).decode()
    assert len(encoded) == BASE64_BLOCK_CHARACTERS and not encoded.endswith("=")
    blocks: list[bytes] = []
    decode_base64(encoded, "$", len(payload), sink=blocks.append)
    assert blocks == [payload]


def test_streaming_rejoins_whitespace_split_across_blocks() -> None:
    payload = _repeating_bytes(200_000)
    wrapped = base64.encodebytes(payload).decode()
    assert "\n" in wrapped
    blocks: list[bytes] = []
    decode_base64(wrapped, "$", len(payload), sink=blocks.append)
    assert b"".join(blocks) == payload


def test_streaming_stops_at_the_decoded_limit_before_the_last_block() -> None:
    payload = _repeating_bytes(199_998)
    encoded = base64.b64encode(payload).decode()
    blocks: list[bytes] = []
    with pytest.raises(PayloadValidationError) as error:
        decode_base64(encoded, "$", len(payload) - 1, sink=blocks.append)
    assert error.value.issues[0].code == "part_too_large"
    assert f"decoded payload is {len(payload)} bytes" in error.value.issues[0].message
    assert 0 < sum(len(block) for block in blocks) < len(payload)


def test_streaming_decodes_url_safe_payloads_across_blocks() -> None:
    payload = _repeating_bytes(200_000)
    url_safe = base64.urlsafe_b64encode(payload).decode()
    assert url_safe != base64.b64encode(payload).decode()
    blocks: list[bytes] = []
    decode_base64(url_safe, "$", len(payload), allow_url_safe=True, sink=blocks.append)
    assert b"".join(blocks) == payload
    with pytest.raises(PayloadValidationError) as error:
        decode_base64(url_safe, "$", len(payload), sink=blocks.append)
    assert error.value.issues[0].code == "url_safe_base64_disabled"


def test_data_url_decoding(png_base64: str) -> None:
    media = decode_data_url(f"data:image/png;base64,{png_base64}", "$", 100)
    assert media.mime_type == "image/png"
    assert detect_mime_type(media.data) == "image/png"


def test_decode_data_url_requires_data_scheme() -> None:
    with pytest.raises(PayloadValidationError) as error:
        decode_data_url("https://example.org/a", "$", 100)
    assert error.value.issues[0].code == "invalid_data_url"


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("data:image/png;base64", "invalid_data_url"),
        ("data:image/png,abc", "data_url_encoding"),
        ("data:image/png;charset=utf-8;base64,AA==", "data_url_parameter"),
        ("data:image/png;base64;base64,AA==", "data_url_parameter"),
        ("data:image/png;;base64,AA==", "data_url_parameter"),
    ],
)
def test_malformed_data_url(value: str, code: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        decode_data_url(value, "$", 100)
    assert error.value.issues[0].code == code


def test_data_url_alphabet_opt_in_is_forwarded(url_safe_png_base64: str) -> None:
    value = f"data:image/png;base64,{url_safe_png_base64}"
    media = decode_data_url(value, "$", 100, allow_url_safe=True)
    assert media.mime_type == "image/png"
    assert detect_mime_type(media.data) == "image/png"
    with pytest.raises(PayloadValidationError) as error:
        decode_data_url(value, "$", 100)
    assert error.value.issues[0].code == "url_safe_base64_disabled"


def test_data_url_header_limit_is_enforced_before_payload_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected_decode(*args: object, **kwargs: object) -> bytes:
        nonlocal called
        called = True
        raise AssertionError("Base64 decoder must not run for an oversized data URL header")

    monkeypatch.setattr("payload_palette.media.decode_base64", unexpected_decode)
    oversized_header = "x" * (MAX_DATA_URL_HEADER_CHARACTERS + 1)
    with pytest.raises(PayloadValidationError) as error:
        decode_data_url(f"data:{oversized_header},AA==", "$", 100)
    assert error.value.issues[0].code == "data_url_header_too_long"
    assert called is False


def test_mime_normalization() -> None:
    assert normalize_mime_type(" IMAGE/PNG ; charset=binary", "$") == "image/png"
    with pytest.raises(PayloadValidationError):
        normalize_mime_type("not a mime", "$")


def test_kind_aware_mp4_format() -> None:
    assert mime_from_format("mp4", "audio") == "audio/mp4"
    assert mime_from_format("mp4", "video") == "video/mp4"
    assert mime_from_format("webm", "audio") == "audio/webm"


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        (b"\xff\xd8\xffrest", "image/jpeg"),
        (b"GIF89arest", "image/gif"),
        (b"OggSrest", "audio/ogg"),
        (b"fLaCrest", "audio/flac"),
        (b"ID3rest", "audio/mpeg"),
        (b"\x00\x00\x00\x18ftypisom", "video/mp4"),
        (b"\x00\x00\x00\x18ftypqt  ", "video/quicktime"),
        (b"\x1aE\xdf\xa3rest", "video/webm"),
        (b"unknown", None),
    ],
)
def test_signature_detection(data: bytes, mime: str | None) -> None:
    assert detect_mime_type(data) == mime


@pytest.mark.parametrize(
    "data",
    [
        b"\x89PNG\r\n\x1a\ntrailing bytes that must not matter",
        b"RIFF\x00\x00\x00\x00WEBPtrailing",
        b"RIFF\x00\x00\x00\x00WAVEtrailing",
        b"\x00\x00\x00\x18ftypisomtrailing",
        b"unknown payload",
    ],
)
def test_signature_detection_needs_only_the_documented_prefix(data: bytes) -> None:
    assert detect_mime_type(data) == detect_mime_type(data[:SIGNATURE_PREFIX_BYTES])
