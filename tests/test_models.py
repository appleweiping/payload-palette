from __future__ import annotations

import json
import sys
from collections.abc import Mapping

import pytest

from payload_palette.models import MAX_MODEL_INTEGER_DIGITS, Manifest, NormalizedPart


def _part(**overrides: object) -> NormalizedPart:
    values: dict[str, object] = {
        "ordinal": 0,
        "path": "$[0]",
        "kind": "text",
        "source": "text",
        "fingerprint": "sha256:abc",
    }
    values.update(overrides)
    return NormalizedPart(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ordinal": -1}, "ordinal"),
        ({"ordinal": True}, "ordinal"),
        ({"ordinal": 10**MAX_MODEL_INTEGER_DIGITS}, "ordinal"),
        ({"kind": "archive"}, "kind"),
        ({"source": "file"}, "source"),
        ({"path": 3}, "path"),
        ({"fingerprint": "bad\ud800value"}, "surrogate"),
        ({"byte_length": True}, "byte_length"),
        ({"character_count": -1}, "character_count"),
        ({"byte_length": 10**MAX_MODEL_INTEGER_DIGITS}, "byte_length"),
        ({"character_count": 10**MAX_MODEL_INTEGER_DIGITS}, "character_count"),
        ({"text": 3}, "text"),
        ({"attributes": []}, "mapping"),
        ({"attributes": {1: "value"}}, "strings"),
        ({"attributes": {"key": "bad\udfffvalue"}}, "surrogate"),
    ],
)
def test_normalized_part_rejects_invalid_runtime_values(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _part(**overrides)


def test_normalized_part_attributes_are_read_only_snapshots() -> None:
    original = {"role": "thumbnail"}
    part = _part(attributes=original)
    original["role"] = "mutated"

    assert isinstance(part.attributes, Mapping)
    assert dict(part.attributes) == {"role": "thumbnail"}
    with pytest.raises(TypeError):
        part.attributes["new"] = "value"  # type: ignore[index]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"parts": ("not-a-part",), "fingerprint": "sha256:x", "inline_bytes": 0},
        {"parts": (), "fingerprint": "sha256:x", "inline_bytes": True},
        {"parts": (), "fingerprint": "sha256:x", "inline_bytes": -1},
        {
            "parts": (),
            "fingerprint": "sha256:x",
            "inline_bytes": 10**MAX_MODEL_INTEGER_DIGITS,
        },
        {"parts": (), "fingerprint": 3, "inline_bytes": 0},
        {"parts": (), "fingerprint": "sha256:x", "inline_bytes": 0, "schema_version": "bad\ud800"},
    ],
)
def test_manifest_rejects_invalid_runtime_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Manifest(**kwargs)  # type: ignore[arg-type]


def test_manifest_freezes_a_sequence_of_parts_as_a_tuple() -> None:
    part = _part()
    manifest = Manifest(parts=[part], fingerprint="sha256:x", inline_bytes=0)  # type: ignore[arg-type]
    assert manifest.parts == (part,)


def test_model_integer_boundary_serializes_at_cpython_minimum_limit() -> None:
    previous_limit = sys.get_int_max_str_digits()
    boundary = (10**MAX_MODEL_INTEGER_DIGITS) - 1
    try:
        sys.set_int_max_str_digits(MAX_MODEL_INTEGER_DIGITS)
        part = _part(
            ordinal=boundary,
            byte_length=boundary,
            character_count=boundary,
        )
        manifest = Manifest(parts=(part,), fingerprint="sha256:x", inline_bytes=boundary)
        encoded = json.dumps(manifest.to_dict(), allow_nan=False)
        assert json.loads(encoded)["inline_bytes"] == boundary
    finally:
        sys.set_int_max_str_digits(previous_limit)
