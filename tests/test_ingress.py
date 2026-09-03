from __future__ import annotations

import json
from io import StringIO

import pytest

from payload_palette import NormalizationPolicy, decode_json_bytes, normalize_json_bytes
from payload_palette.cli import _read_json
from payload_palette.errors import PayloadValidationError
from payload_palette.ingress import MAX_JSON_DEPTH, validate_input_limit


def _first_code(error: PayloadValidationError) -> str:
    return error.issues[0].code


def test_ingress_decodes_bytes_like_bodies_and_normalizes() -> None:
    raw = b'{"content":[{"type":"text","text":"hello"}]}'

    assert decode_json_bytes(raw)["content"][0]["text"] == "hello"
    assert decode_json_bytes(bytearray(raw)) == decode_json_bytes(memoryview(raw))
    manifest = normalize_json_bytes(raw)
    assert manifest.part_count == 1
    assert manifest.parts[0].text == "hello"


@pytest.mark.parametrize("value", [True, 0, -1, 512 * 1024 * 1024 + 1, 1.5, "10"])
def test_ingress_limit_requires_a_bounded_real_integer(value: object) -> None:
    with pytest.raises(ValueError, match="max_input_bytes"):
        validate_input_limit(value)


def test_ingress_rejects_oversized_body_before_json_decode() -> None:
    with pytest.raises(PayloadValidationError) as caught:
        decode_json_bytes(b"not-json", max_input_bytes=7)
    assert _first_code(caught.value) == "input_too_large"


def test_ingress_rejects_non_bytes_and_multidimensional_views() -> None:
    with pytest.raises(PayloadValidationError) as caught:
        decode_json_bytes("{}")  # type: ignore[arg-type]
    assert _first_code(caught.value) == "input_type"


def test_ingress_wraps_released_memoryview_as_stable_validation_error() -> None:
    released = memoryview(b"{}")
    released.release()

    with pytest.raises(PayloadValidationError) as caught:
        decode_json_bytes(released)

    assert _first_code(caught.value) == "input_type"


def test_normalize_json_bytes_defaults_only_none_and_validates_policy_first() -> None:
    class FalseyPolicy(NormalizationPolicy):
        def __bool__(self) -> bool:
            return False

    falsey = FalseyPolicy(max_text_characters=0)
    raw = b'[{"type":"text","text":"not-empty"}]'
    with pytest.raises(PayloadValidationError) as caught:
        normalize_json_bytes(raw, falsey)
    assert _first_code(caught.value) == "text_too_long"

    with pytest.raises(ValueError, match="NormalizationPolicy or None"):
        normalize_json_bytes(b"not-json", False)  # type: ignore[arg-type]

    grid = memoryview(b"1234").cast("B", shape=[2, 2])
    with pytest.raises(PayloadValidationError) as caught:
        decode_json_bytes(grid)
    assert _first_code(caught.value) == "input_type"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"a":1,"a":2}', "duplicate_json_key"),
        (b'{"n":NaN}', "nonstandard_json_number"),
        (b'{"n":1e400}', "json_number_range"),
        (b'{"n":' + b"9" * 257 + b"}", "json_number_range"),
        (b'{"text":"\\ud800"}', "invalid_unicode"),
        (b"\xff", "input_encoding"),
    ],
)
def test_ingress_and_cli_share_strict_json_failures(raw: bytes, code: str) -> None:
    with pytest.raises(PayloadValidationError) as ingress_error:
        decode_json_bytes(raw)
    with pytest.raises(PayloadValidationError) as cli_error:
        _read_json("-", StringIO(raw.decode("utf-8", errors="surrogateescape")))

    assert _first_code(ingress_error.value) == code
    if code != "input_encoding":
        assert _first_code(cli_error.value) == code


def test_ingress_rejects_excessive_depth_but_ignores_brackets_in_strings() -> None:
    assert decode_json_bytes(json.dumps(["[" * 200]).encode("utf-8")) == ["[" * 200]
    raw = ("[" * (MAX_JSON_DEPTH + 1) + "0" + "]" * (MAX_JSON_DEPTH + 1)).encode()
    with pytest.raises(PayloadValidationError) as caught:
        decode_json_bytes(raw)
    assert _first_code(caught.value) == "json_too_deep"
