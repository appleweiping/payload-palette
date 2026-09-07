from __future__ import annotations

import base64
import io
import json
import tracemalloc
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from typing import Any

import pytest

from payload_palette import (
    LARGE_VALUE_PREFIX_CHARACTERS,
    LargeValue,
    NormalizationPolicy,
    RemoteURLPolicy,
    decode_stream,
    normalize,
    normalize_json_bytes,
    normalize_path,
    normalize_stream,
)
from payload_palette.cli import run
from payload_palette.errors import PayloadValidationError
from payload_palette.ingress import _decode_json_text
from payload_palette.jsonstream import MAX_JSON_DEPTH, parse_json_stream
from payload_palette.media import Base64Digester, decode_base64
from payload_palette.models import value_length, value_prefix
from payload_palette.streaming import large_value_threshold

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _first_code(error: PayloadValidationError) -> str:
    return error.issues[0].code


def _codes(error: PayloadValidationError) -> tuple[str, ...]:
    return tuple(issue.code for issue in error.issues)


def _stream(raw: bytes, chunk: int = 1 << 16) -> io.RawIOBase:
    """A byte stream that hands out at most ``chunk`` bytes per read."""

    class Chunked(io.RawIOBase):
        def __init__(self) -> None:
            self._at = 0

        def read(self, _size: int = -1) -> bytes:
            block = raw[self._at : self._at + chunk]
            self._at += len(block)
            return block

    return Chunked()


def _parse(raw: bytes, *, chunk: int = 1 << 16, threshold: int = 4096) -> Any:
    document, _statistics = parse_json_stream(
        _stream(raw, chunk),
        max_input_bytes=1 << 26,
        large_value_threshold=threshold,
        media_max_bytes=1 << 26,
    )
    return document


def _png_base64(payload_size: int) -> str:
    return base64.b64encode(PNG_HEADER + bytes(payload_size)).decode()


def _request(url: str) -> bytes:
    document = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
    }
    return json.dumps(document).encode()


def _anthropic(source: dict[str, str]) -> bytes:
    document = {"messages": [{"role": "user", "content": [{"type": "image", "source": source}]}]}
    return json.dumps(document).encode()


# --------------------------------------------------------------------------
# The streamed and buffered decoders answer the same question.
# --------------------------------------------------------------------------

DOCUMENTS = [
    b"{}",
    b"[]",
    b'{"a":1,"b":[1,2,{"c":"x"}],"d":null,"e":true,"f":false}',
    b'{"n":[0,-0,1e3,-1.5,2.5e-7,12345678901234567890]}',
    b'  \n\t {"spaced" : [ 1 , 2 ] }  \n ',
    b'"top-level string"',
    b"1234",
    b"null",
    '{"unicode":"café 中文 \U0001f600"}'.encode(),
    b'{"escapes":"quote\\" back\\\\ slash\\/ b\\b f\\f n\\n r\\r t\\t u\\u00e9"}',
    b'{"surrogate pair":"\\ud83d\\ude00"}',
    b'{"empty":"","nested":{"deep":{"deeper":[[[]]]}}}',
]


@pytest.mark.parametrize("raw", DOCUMENTS)
@pytest.mark.parametrize("chunk", [1, 2, 7, 4096])
def test_streamed_decode_matches_the_buffered_decoder(raw: bytes, chunk: int) -> None:
    assert _parse(raw, chunk=chunk) == _decode_json_text(raw.decode("utf-8"))


REFUSALS = [
    (b"", "invalid_json"),
    (b"   ", "invalid_json"),
    (b"{", "invalid_json"),
    (b'{"a"}', "invalid_json"),
    (b'{"a":}', "invalid_json"),
    (b'{"a":1,}', "invalid_json"),
    (b"[1 2]", "invalid_json"),
    (b'{"a":1} trailing', "invalid_json"),
    (b'{"a":"unterminated', "invalid_json"),
    (b'{"a":tru}', "invalid_json"),
    (b'{"a":01}', "invalid_json"),
    (b'{"a":+1}', "invalid_json"),
    (b'{"a":.5}', "invalid_json"),
    (b'{"a":1e}', "invalid_json"),
    (b"{1:2}", "invalid_json"),
    (b'{"a":"\\q"}', "invalid_json"),
    (b'{"a":"\\u00zz"}', "invalid_json"),
    (b'{"a":"raw\nnewline"}', "invalid_json"),
    (b'{"a":NaN}', "nonstandard_json_number"),
    (b'{"a":Infinity}', "nonstandard_json_number"),
    (b'{"a":-Infinity}', "nonstandard_json_number"),
    (b'{"a":1,"a":2}', "duplicate_json_key"),
    (b'{"a":"\\ud800"}', "invalid_unicode"),
    (b'{"a":"\\udc00"}', "invalid_unicode"),
    (b'{"a":"\\ud800\\u0041"}', "invalid_unicode"),
    (b'{"a":' + b"9" * 300 + b"}", "json_number_range"),
    (b'{"a":1e400}', "json_number_range"),
    (b"[" * 200 + b"]" * 200, "json_too_deep"),
    (b'{"a":"\xff\xfe"}', "input_encoding"),
]


@pytest.mark.parametrize(("raw", "code"), REFUSALS)
@pytest.mark.parametrize("chunk", [1, 3, 4096])
def test_streamed_decode_refuses_with_a_stable_code(raw: bytes, code: str, chunk: int) -> None:
    with pytest.raises(PayloadValidationError) as caught:
        _parse(raw, chunk=chunk)
    assert _first_code(caught.value) == code


def test_streamed_decode_accepts_exactly_the_maximum_nesting() -> None:
    at_limit = b"[" * MAX_JSON_DEPTH + b"]" * MAX_JSON_DEPTH
    assert _parse(at_limit) == _decode_json_text(at_limit.decode())
    with pytest.raises(PayloadValidationError) as caught:
        _parse(b"[" * (MAX_JSON_DEPTH + 1) + b"]" * (MAX_JSON_DEPTH + 1))
    assert _first_code(caught.value) == "json_too_deep"


def test_streamed_decode_bounds_the_input() -> None:
    with pytest.raises(PayloadValidationError) as caught:
        parse_json_stream(
            _stream(b'{"a":"' + b"x" * 5000 + b'"}'),
            max_input_bytes=64,
            large_value_threshold=4096,
            media_max_bytes=1 << 20,
        )
    assert _first_code(caught.value) == "input_too_large"


def test_streamed_decode_reports_the_position_of_a_fault_across_chunks() -> None:
    raw = b'{\n  "a": 1,\n  "b": [1, 2,,]\n}'
    with pytest.raises(PayloadValidationError) as caught:
        _parse(raw, chunk=4)
    assert "line 3" in caught.value.issues[0].message


@pytest.mark.parametrize("value", [b"{}", b"[]", b"null"])
def test_streamed_decode_rejects_a_non_bytes_stream(value: bytes) -> None:
    class TextStream(io.RawIOBase):
        def read(self, _size: int = -1) -> Any:
            return value.decode()

    with pytest.raises(PayloadValidationError) as caught:
        parse_json_stream(
            TextStream(),
            max_input_bytes=1 << 20,
            large_value_threshold=4096,
            media_max_bytes=1 << 20,
        )
    assert _first_code(caught.value) == "input_type"


def test_streamed_decode_reports_an_unreadable_stream() -> None:
    class Broken(io.RawIOBase):
        def read(self, _size: int = -1) -> bytes:
            raise OSError("device disappeared")

    with pytest.raises(PayloadValidationError) as caught:
        parse_json_stream(
            Broken(),
            max_input_bytes=1 << 20,
            large_value_threshold=4096,
            media_max_bytes=1 << 20,
        )
    assert _first_code(caught.value) == "input_read"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_input_bytes", 0),
        ("max_input_bytes", 1.5),
        ("large_value_threshold", LARGE_VALUE_PREFIX_CHARACTERS),
        ("large_value_threshold", True),
        ("media_max_bytes", -1),
    ],
)
def test_streamed_decode_validates_its_own_limits(field: str, value: object) -> None:
    arguments: dict[str, Any] = {
        "max_input_bytes": 1 << 20,
        "large_value_threshold": 4096,
        "media_max_bytes": 1 << 20,
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=field):
        parse_json_stream(_stream(b"{}"), **arguments)


# --------------------------------------------------------------------------
# An over-long string becomes a measurement rather than characters.
# --------------------------------------------------------------------------


def test_a_long_string_is_kept_as_a_measured_value() -> None:
    encoded = _png_base64(9000)
    raw = json.dumps({"image": encoded}).encode()
    document = _parse(raw)
    value = document["image"]

    assert isinstance(value, LargeValue)
    assert value.character_count == len(encoded)
    assert value.prefix == encoded[:LARGE_VALUE_PREFIX_CHARACTERS]
    assert value.media is not None
    assert value.media.byte_length == len(PNG_HEADER) + 9000
    assert value.media.signature_prefix.startswith(PNG_HEADER)
    assert value.media_issue is None


def test_a_short_string_keeps_its_characters() -> None:
    document = _parse(json.dumps({"image": _png_base64(8)}).encode())
    assert isinstance(document["image"], str)


def test_a_measured_value_carries_the_data_url_header() -> None:
    encoded = _png_base64(9000)
    document = _parse(json.dumps({"u": f"data:image/png;base64,{encoded}"}).encode())
    value = document["u"]
    assert isinstance(value, LargeValue)
    assert value.data_url_header == "data:image/png;base64,"
    assert value.media is not None
    assert value.media.byte_length == len(PNG_HEADER) + 9000


def test_a_measured_value_defers_a_base64_refusal() -> None:
    document = _parse(json.dumps({"image": "!" * 9000}).encode())
    value = document["image"]
    assert isinstance(value, LargeValue)
    assert value.media is None
    assert value.media_issue is not None
    assert value.media_issue.code == "invalid_base64"
    raised = value.media_issue.raised_at("$.content[0].data")
    assert raised.issues[0].path == "$.content[0].data"


def test_a_measured_value_rejects_an_unterminated_data_url_header() -> None:
    document = _parse(json.dumps({"u": "data:" + "a" * 9000}).encode())
    value = document["u"]
    assert isinstance(value, LargeValue)
    assert value.media_issue is not None
    assert value.media_issue.code == "data_url_header_too_long"


def test_a_value_carries_either_a_summary_or_a_refusal() -> None:
    with pytest.raises(ValueError, match="either"):
        LargeValue(
            character_count=LARGE_VALUE_PREFIX_CHARACTERS + 1,
            prefix="x" * LARGE_VALUE_PREFIX_CHARACTERS,
        )


@pytest.mark.parametrize("chunk", [1, 2, 3, 17, 4096, 1 << 18])
def test_a_measurement_does_not_depend_on_the_chunk_size(chunk: int) -> None:
    encoded = _png_base64(20_000)
    raw = json.dumps({"image": encoded}).encode()
    value = _parse(raw, chunk=chunk)["image"]
    assert isinstance(value, LargeValue)
    assert value.media is not None
    assert value.character_count == len(encoded)
    assert value.media.digest == _parse(raw, chunk=1 << 20)["image"].media.digest


def test_statistics_report_what_the_parse_retained() -> None:
    encoded = _png_base64(50_000)
    raw = json.dumps({"t": "short", "image": encoded}).encode()
    _document, statistics = parse_json_stream(
        _stream(raw),
        max_input_bytes=1 << 26,
        large_value_threshold=4096,
        media_max_bytes=1 << 26,
    )
    assert statistics.byte_count == len(raw)
    assert statistics.large_values == 1
    assert statistics.largest_value_characters == len(encoded)
    assert statistics.streamed_media_bytes == len(PNG_HEADER) + 50_000
    assert statistics.peak_retained_characters < 100


# --------------------------------------------------------------------------
# Measured values reaching the rest of the pipeline.
# --------------------------------------------------------------------------


def test_value_accessors_agree_for_both_representations() -> None:
    document = _parse(json.dumps({"image": _png_base64(9000)}).encode())
    measured = document["image"]
    assert value_length(measured) == measured.character_count
    assert value_prefix(measured, 5) == measured.prefix[:5]
    assert value_length("abc") == 3
    assert value_prefix("abcdef", 3) == "abc"
    with pytest.raises(ValueError, match="retained"):
        value_prefix(measured, LARGE_VALUE_PREFIX_CHARACTERS + 1)


def test_an_over_long_text_part_is_refused_with_its_true_length() -> None:
    policy = NormalizationPolicy(max_text_characters=32)
    text = "x" * (large_value_threshold(policy) + 5)
    raw = json.dumps({"content": [{"type": "text", "text": text}]}).encode()
    with pytest.raises(PayloadValidationError) as caught:
        normalize_stream(io.BytesIO(raw), policy)
    assert _first_code(caught.value) == "text_too_long"
    assert str(len(text)) in caught.value.issues[0].message


def test_an_over_long_remote_url_is_refused() -> None:
    policy = NormalizationPolicy(remote=RemoteURLPolicy(allowed_hosts=("example.org",)))
    url = "https://example.org/" + "a" * large_value_threshold(policy)
    raw = _request(url)
    with pytest.raises(PayloadValidationError) as caught:
        normalize_stream(io.BytesIO(raw), policy)
    assert _first_code(caught.value) == "url_too_long"


def test_a_streamed_data_url_is_normalized_like_a_buffered_one() -> None:
    raw = _request(f"data:image/png;base64,{_png_base64(300_000)}")
    streamed = normalize_stream(io.BytesIO(raw))
    buffered = normalize_json_bytes(raw)
    assert streamed.to_dict() == buffered.to_dict()
    assert streamed.parts[1].byte_length == len(PNG_HEADER) + 300_000


def test_a_streamed_data_url_still_detects_a_declaration_conflict() -> None:
    encoded = _png_base64(300_000)
    raw = _anthropic(
        {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": f"data:image/png;base64,{encoded}",
        }
    )
    policy = NormalizationPolicy(envelope="anthropic")
    with pytest.raises(PayloadValidationError) as streamed:
        normalize_stream(io.BytesIO(raw), policy)
    with pytest.raises(PayloadValidationError) as buffered:
        normalize_json_bytes(raw, policy)
    assert _codes(streamed.value) == ("mime_conflict",)
    assert _codes(streamed.value) == _codes(buffered.value)


def test_a_streamed_signature_mismatch_is_refused() -> None:
    wrong = base64.b64encode(b"\xff\xd8\xff" + bytes(300_000)).decode()
    raw = _anthropic({"type": "base64", "media_type": "image/png", "data": wrong})
    with pytest.raises(PayloadValidationError) as caught:
        normalize_stream(io.BytesIO(raw), NormalizationPolicy(envelope="anthropic"))
    assert _first_code(caught.value) == "signature_mismatch"


def test_a_streamed_part_over_its_kind_limit_is_refused() -> None:
    policy = NormalizationPolicy(
        max_bytes_by_kind={"text": 1 << 20, "image": 1024, "audio": 1 << 20, "video": 1 << 20}
    )
    raw = _request(f"data:image/png;base64,{_png_base64(300_000)}")
    with pytest.raises(PayloadValidationError) as caught:
        normalize_stream(io.BytesIO(raw), policy)
    assert _first_code(caught.value) == "part_too_large"


def test_a_streamed_invalid_base64_is_reported_at_the_content_path() -> None:
    raw = _request("data:image/png;base64," + "!" * 300_000)
    with pytest.raises(PayloadValidationError) as caught:
        normalize_stream(io.BytesIO(raw))
    issue = caught.value.issues[0]
    assert issue.code == "invalid_base64"
    assert issue.path == "$.messages[0].content[1].image_url.url"


def test_a_streamed_undeclared_image_is_recognized_by_its_signature() -> None:
    encoded = _png_base64(300_000)
    raw = json.dumps({"prompt": "look", "images": [encoded]}).encode()
    policy = NormalizationPolicy(envelope="ollama")
    assert (
        normalize_stream(io.BytesIO(raw), policy).to_dict()
        == normalize_json_bytes(raw, policy).to_dict()
    )


def test_url_safe_streamed_media_follows_the_policy() -> None:
    # Zero-filled media encodes to the same characters in both alphabets, which
    # would make this test pass without exercising url-safety at all.
    body = bytes(range(256)) * 1200
    assert "+" in base64.b64encode(PNG_HEADER + body).decode()
    assert "/" in base64.b64encode(PNG_HEADER + body).decode()
    encoded = base64.urlsafe_b64encode(PNG_HEADER + body).decode()
    raw = _request(f"data:image/png;base64,{encoded}")
    with pytest.raises(PayloadValidationError) as caught:
        normalize_stream(io.BytesIO(raw))
    assert _first_code(caught.value) == "url_safe_base64_disabled"
    permissive = NormalizationPolicy(allow_url_safe_base64=True)
    assert normalize_stream(io.BytesIO(raw), permissive).parts[1].byte_length == (
        len(PNG_HEADER) + len(body)
    )


def test_the_threshold_stays_above_every_limit_that_needs_characters() -> None:
    policy = NormalizationPolicy(max_text_characters=0)
    threshold = large_value_threshold(policy)
    assert threshold > LARGE_VALUE_PREFIX_CHARACTERS
    assert threshold > policy.max_text_characters
    generous = NormalizationPolicy(max_text_characters=5_000_000)
    assert large_value_threshold(generous) > generous.max_text_characters


# --------------------------------------------------------------------------
# Entry points.
# --------------------------------------------------------------------------


def test_decode_stream_returns_the_document_and_its_statistics() -> None:
    raw = _request(f"data:image/png;base64,{_png_base64(300_000)}")
    document, statistics = decode_stream(io.BytesIO(raw))
    assert statistics.large_values == 1
    assert normalize(document).to_dict() == normalize_json_bytes(raw).to_dict()


def test_normalize_path_reads_one_file(tmp_path: Path) -> None:
    raw = _request(f"data:image/png;base64,{_png_base64(300_000)}")
    source = tmp_path / "request.json"
    source.write_bytes(raw)
    assert normalize_path(source).to_dict() == normalize_json_bytes(raw).to_dict()


def test_normalize_path_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PayloadValidationError) as caught:
        normalize_path(tmp_path / "absent.json")
    assert _first_code(caught.value) == "input_read"


@pytest.mark.parametrize("entry", [normalize_stream, decode_stream])
def test_stream_entry_points_reject_a_foreign_policy(entry: Any) -> None:
    with pytest.raises(ValueError, match="NormalizationPolicy"):
        entry(io.BytesIO(b"{}"), policy=object())


# --------------------------------------------------------------------------
# The streaming Base64 digester answers exactly as the buffered decoder does.
# --------------------------------------------------------------------------


def _buffered_summary(value: str, *, allow: bool = False, cap: int = 1 << 20) -> tuple[int, bytes]:
    blocks: list[bytes] = []
    decode_base64(value, "$.v", cap, allow_url_safe=allow, sink=blocks.append)
    joined = b"".join(blocks)
    return len(joined), joined[:12]


@pytest.mark.parametrize("size", [0, 1, 2, 3, 47, 48, 49, 3000])
@pytest.mark.parametrize("chunk", [1, 3, 4, 64, 1 << 17])
def test_the_digester_matches_the_buffered_decoder(size: int, chunk: int) -> None:
    raw = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    encoded = base64.b64encode(raw).decode()
    digester = Base64Digester("$.v", 1 << 20)
    for start in range(0, len(encoded), chunk):
        digester.feed(encoded[start : start + chunk])
    if not raw:
        with pytest.raises(PayloadValidationError) as caught:
            digester.finish()
        assert _first_code(caught.value) == "empty_base64"
        return
    summary = digester.finish()
    assert (summary.byte_length, summary.signature_prefix) == _buffered_summary(encoded)


DIGESTER_REFUSALS = [
    ("!!!!", False, "invalid_base64"),
    ("QUJDR", False, "invalid_base64"),
    ("QQF", False, "invalid_base64"),
    ("A", False, "invalid_base64"),
    ("QUJD=", False, "invalid_base64"),
    ("QQ==QQ==", False, "invalid_base64"),
    ("QUJDR-Q_", False, "url_safe_base64_disabled"),
    ("QUJDR-Q/", False, "mixed_base64_alphabet"),
    ("QUJDR-Q/", True, "mixed_base64_alphabet"),
    ("", False, "empty_base64"),
    ("   ", False, "empty_base64"),
]


@pytest.mark.parametrize(("value", "allow", "code"), DIGESTER_REFUSALS)
@pytest.mark.parametrize("chunk", [1, 2, 8192])
def test_the_digester_refuses_like_the_buffered_decoder(
    value: str, allow: bool, code: str, chunk: int
) -> None:
    digester = Base64Digester("$.v", 1 << 20, allow_url_safe=allow)
    with pytest.raises(PayloadValidationError) as caught:
        for start in range(0, len(value), chunk):
            digester.feed(value[start : start + chunk])
        digester.finish()
    assert _first_code(caught.value) == code
    with pytest.raises(PayloadValidationError) as buffered:
        decode_base64(value, "$.v", 1 << 20, allow_url_safe=allow)
    assert _first_code(buffered.value) == code


def test_the_digester_bounds_the_decoded_size() -> None:
    encoded = base64.b64encode(bytes(4096)).decode()
    digester = Base64Digester("$.v", 64)
    with pytest.raises(PayloadValidationError) as caught:
        digester.feed(encoded)
        digester.finish()
    assert _first_code(caught.value) == "part_too_large"


def test_the_digester_bounds_whitespace_padding() -> None:
    digester = Base64Digester("$.v", 16)
    with pytest.raises(PayloadValidationError) as caught:
        digester.feed(" " * 100_000)
    assert _first_code(caught.value) == "part_too_large"


def test_the_digester_ignores_whitespace_between_blocks() -> None:
    raw = bytes(range(200))
    encoded = base64.b64encode(raw).decode()
    spaced = "\n".join(encoded[index : index + 7] for index in range(0, len(encoded), 7))
    digester = Base64Digester("$.v", 1 << 20)
    digester.feed(spaced)
    assert digester.finish().byte_length == len(raw)


def test_the_digester_refuses_reuse() -> None:
    digester = Base64Digester("$.v", 1 << 20)
    digester.feed(base64.b64encode(b"abc").decode())
    digester.finish()
    with pytest.raises(RuntimeError, match="finish"):
        digester.feed("QQ==")
    with pytest.raises(RuntimeError, match="finish"):
        digester.finish()


# --------------------------------------------------------------------------
# The reason the feature exists.
# --------------------------------------------------------------------------


def _peak_bytes(work: Any) -> int:
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        work()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _peaks(media_bytes: int) -> tuple[int, int]:
    policy = NormalizationPolicy(
        max_bytes_by_kind={"text": 1 << 20, "image": 1 << 30, "audio": 1 << 20, "video": 1 << 20}
    )
    raw = _request(f"data:image/png;base64,{_png_base64(media_bytes)}")
    streamed = _peak_bytes(lambda: normalize_stream(io.BytesIO(raw), policy))
    buffered = _peak_bytes(lambda: normalize_json_bytes(raw, policy))
    return streamed, buffered


def test_streaming_peak_memory_does_not_follow_the_media_size() -> None:
    """The point of the feature: peak allocation stops tracking the payload.

    Asserting an absolute ceiling would only pin the chunk size, so this
    measures the *slope* instead. Quadrupling the media must leave the streamed
    peak essentially where it was while the buffered peak grows with the
    request, which is exactly the difference between holding a payload and
    summarizing it.
    """

    small_streamed, small_buffered = _peaks(1 << 20)
    large_streamed, large_buffered = _peaks(4 << 20)

    assert large_streamed < small_streamed * 1.25
    assert large_buffered > small_buffered * 2
    assert large_streamed < large_buffered // 4


# --------------------------------------------------------------------------
# Reader and grammar corners.
# --------------------------------------------------------------------------


def test_a_stream_yielding_an_empty_string_is_treated_as_end_of_input() -> None:
    class EmptyText(io.RawIOBase):
        def __init__(self) -> None:
            self._served = False

        def read(self, _size: int = -1) -> Any:
            if self._served:
                return ""
            self._served = True
            return b"{}"

    document, _statistics = parse_json_stream(
        EmptyText(),
        max_input_bytes=1 << 20,
        large_value_threshold=4096,
        media_max_bytes=1 << 20,
    )
    assert document == {}


def test_a_multibyte_character_split_across_chunks_is_decoded() -> None:
    raw = '{"k":"é中文\U0001f600"}'.encode()
    for chunk in range(1, 8):
        assert _parse(raw, chunk=chunk) == {"k": "é中文\U0001f600"}


def test_a_truncated_multibyte_sequence_is_reported_as_an_encoding_fault() -> None:
    raw = '{"k":"中"}'.encode()[:-3]
    with pytest.raises(PayloadValidationError) as caught:
        _parse(raw, chunk=2)
    assert _first_code(caught.value) == "input_encoding"


@pytest.mark.parametrize(
    "raw",
    [b"[", b'{"a":', rb'{"a":"\u00', rb'{"a":"\\', b"[1,", b'{"a":1 2}', b'{"a":1'],
)
@pytest.mark.parametrize("chunk", [1, 4096])
def test_a_truncated_document_is_refused(raw: bytes, chunk: int) -> None:
    with pytest.raises(PayloadValidationError) as caught:
        _parse(raw, chunk=chunk)
    assert _first_code(caught.value) in {"invalid_json", "invalid_unicode"}


def test_a_fault_after_a_newline_inside_one_chunk_reports_its_line() -> None:
    with pytest.raises(PayloadValidationError) as caught:
        _parse(b'{\n\n\n  "a": @\n}', chunk=1 << 16)
    assert "line 4" in caught.value.issues[0].message


def test_statistics_track_the_largest_of_several_measured_values() -> None:
    small = _png_base64(6_000)
    large = _png_base64(30_000)
    raw = json.dumps({"a": small, "b": large, "c": small}).encode()
    _document, statistics = parse_json_stream(
        _stream(raw),
        max_input_bytes=1 << 26,
        large_value_threshold=4096,
        media_max_bytes=1 << 26,
    )
    assert statistics.large_values == 3
    assert statistics.largest_value_characters == len(large)
    assert statistics.streamed_media_bytes == 2 * (len(PNG_HEADER) + 6_000) + (
        len(PNG_HEADER) + 30_000
    )


# --------------------------------------------------------------------------
# The command line.
# --------------------------------------------------------------------------


def test_cli_streams_a_file(tmp_path: Path) -> None:
    source = tmp_path / "request.json"
    source.write_bytes(_request(f"data:image/png;base64,{_png_base64(300_000)}"))
    streamed, buffered = StringIO(), StringIO()
    assert run(["validate", str(source), "--stream"], stdout=streamed, stderr=StringIO()) == 0
    assert run(["validate", str(source)], stdout=buffered, stderr=StringIO()) == 0
    assert json.loads(streamed.getvalue()) == json.loads(buffered.getvalue())


def test_cli_streams_standard_input_through_its_binary_buffer() -> None:
    raw = _request(f"data:image/png;base64,{_png_base64(300_000)}")
    stdin = TextIOWrapper(BytesIO(raw), encoding="utf-8")
    stdout = StringIO()
    assert run(["validate", "-", "--stream"], stdin=stdin, stdout=stdout, stderr=StringIO()) == 0
    assert json.loads(stdout.getvalue())["part_count"] == 2


def test_cli_refuses_to_stream_a_text_only_standard_input() -> None:
    stderr = StringIO()
    code = run(
        ["validate", "-", "--stream", "--json-errors"],
        stdin=StringIO("{}"),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "input_read"


def test_cli_streaming_reports_a_missing_file(tmp_path: Path) -> None:
    stderr = StringIO()
    code = run(
        ["validate", str(tmp_path / "absent.json"), "--stream", "--json-errors"],
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "input_read"


def test_a_fault_before_any_refill_reports_its_line_and_column() -> None:
    # A missing colon is refused without first pulling another chunk, so the
    # position comes from the live buffer rather than the carried-over counters.
    with pytest.raises(PayloadValidationError) as caught:
        _parse(b'{\n\n\n  "a" 1\n}', chunk=1 << 16)
    message = caught.value.issues[0].message
    assert "line 4" in message
    assert "column 7" in message


def test_a_part_spec_refuses_a_value_that_is_neither_text_nor_measured() -> None:
    from payload_palette.models import PartSpec

    with pytest.raises(ValueError, match="string or a streamed"):
        PartSpec(
            ordinal=0,
            path="$",
            value_path="$.text",
            kind="text",
            value=123,  # type: ignore[arg-type]
            source_hint="text",
        )
