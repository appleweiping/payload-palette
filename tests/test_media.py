from __future__ import annotations

import base64

import pytest

from payload_palette.errors import PayloadValidationError
from payload_palette.media import (
    MAX_DATA_URL_HEADER_CHARACTERS,
    decode_base64,
    decode_data_url,
    detect_mime_type,
    mime_from_format,
    normalize_mime_type,
)


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
