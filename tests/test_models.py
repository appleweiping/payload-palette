from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace

import pytest

import payload_palette.models as model_module
from payload_palette import LARGE_VALUE_PREFIX_CHARACTERS, LargeValue, normalize
from payload_palette.media import Base64Summary
from payload_palette.models import (
    MAX_MODEL_INTEGER_DIGITS,
    DeferredIssue,
    Manifest,
    NormalizedPart,
    value_length,
    value_prefix,
)


def _part(**overrides: object) -> NormalizedPart:
    values: dict[str, object] = {
        "ordinal": 0,
        "path": "$[0]",
        "kind": "text",
        "source": "text",
        "fingerprint": f"sha256:{hashlib.sha256(b'').hexdigest()}",
        "byte_length": 0,
        "character_count": 0,
        "mime_type": "text/plain",
        "text": "",
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
        ({"path": "not-a-path"}, "path"),
        ({"fingerprint": "sha256:" + "0" * 64}, "fingerprint"),
        ({"fingerprint": "bad\ud800value"}, "surrogate"),
        ({"byte_length": True}, "byte_length"),
        ({"character_count": -1}, "character_count"),
        ({"byte_length": 10**MAX_MODEL_INTEGER_DIGITS}, "byte_length"),
        ({"character_count": 10**MAX_MODEL_INTEGER_DIGITS}, "character_count"),
        ({"text": 3}, "text"),
        ({"mime_type": "Text/Plain"}, "mime_type"),
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


def test_attribute_copying_is_bounded_without_trusting_mapping_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EndlessAttributes(Mapping[str, str]):
        def __init__(self) -> None:
            self.items_requested = 0

        def __getitem__(self, key: str) -> str:
            return "value"

        def __iter__(self) -> Iterator[str]:
            index = 0
            while True:
                self.items_requested += 1
                yield f"attribute-{index}"
                index += 1

        def __len__(self) -> int:
            return 0

    attributes = EndlessAttributes()
    monkeypatch.setattr(model_module, "MAX_PART_ATTRIBUTES", 2)

    with pytest.raises(ValueError, match="at most 2"):
        _part(attributes=attributes)
    assert attributes.items_requested == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "image"},
        {"source": "inline"},
        {"byte_length": 1},
        {"character_count": 1},
        {"mime_type": "image/png"},
        {"locator": "inline"},
    ],
)
def test_text_part_rejects_every_inconsistent_shape(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _part(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"byte_length": None},
        {"byte_length": 0},
        {"mime_type": None},
        {"locator": None},
        {"locator": "elsewhere"},
        {"text": "payload"},
        {"character_count": 7},
    ],
)
def test_inline_part_rejects_every_inconsistent_shape(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "ordinal": 0,
        "path": "$[0]",
        "kind": "image",
        "source": "inline",
        "fingerprint": "sha256:" + "1" * 64,
        "byte_length": 8,
        "mime_type": "image/png",
        "locator": "inline",
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        NormalizedPart(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"byte_length": 1},
        {"locator": None},
        {"locator": ""},
        {"text": "payload"},
        {"character_count": 7},
    ],
)
def test_remote_part_rejects_every_inconsistent_shape(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "ordinal": 0,
        "path": "$[0]",
        "kind": "image",
        "source": "remote",
        "fingerprint": "sha256:" + "2" * 64,
        "locator": "https://example.test/image.png",
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        NormalizedPart(**values)  # type: ignore[arg-type]


def _large_value(**overrides: object) -> LargeValue:
    values: dict[str, object] = {
        "character_count": LARGE_VALUE_PREFIX_CHARACTERS + 1,
        "prefix": "A" * LARGE_VALUE_PREFIX_CHARACTERS,
        "media": Base64Summary(1, "0" * 64, b"x"),
    }
    values.update(overrides)
    return LargeValue(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("code", "message", "hint"),
    [
        ("", "message", None),
        ("code", "", None),
        (3, "message", None),
        ("code", "message", "bad\ud800hint"),
    ],
)
def test_deferred_issue_rejects_invalid_direct_construction(
    code: object, message: object, hint: object
) -> None:
    with pytest.raises(ValueError):
        DeferredIssue(code, message, hint)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"character_count": True},
        {"character_count": -1},
        {"character_count": LARGE_VALUE_PREFIX_CHARACTERS},
        {"character_count": 10**MAX_MODEL_INTEGER_DIGITS},
        {"prefix": 3},
        {"prefix": "short"},
        {"prefix": ("A" * (LARGE_VALUE_PREFIX_CHARACTERS - 1)) + "\ud800"},
        {"data_url_header": "not-data,"},
        {"data_url_header": "data:image/png;base64"},
        {"data_url_header": "data:image/png;base64,"},
        {"media": object()},
        {"media": None, "media_issue": object()},
        {"media_issue": DeferredIssue("invalid_base64", "invalid")},
    ],
)
def test_large_value_rejects_invalid_direct_construction(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _large_value(**overrides)


def test_large_value_accepts_a_consistent_data_url_summary() -> None:
    header = "data:image/png;base64,"
    value = _large_value(
        prefix=header + "A" * (LARGE_VALUE_PREFIX_CHARACTERS - len(header)),
        data_url_header=header,
    )
    assert value.data_url_header == header


@pytest.mark.parametrize(
    "header",
    [
        "data:image/png;name=payload;base64,",
        "data:" + ("a" * 1025) + ",",
    ],
)
def test_large_value_applies_the_shared_data_url_header_grammar(header: str) -> None:
    prefix = header + "A" * (LARGE_VALUE_PREFIX_CHARACTERS - len(header))

    with pytest.raises(ValueError):
        _large_value(prefix=prefix, data_url_header=header)


def test_large_value_revalidates_dataclass_replace() -> None:
    with pytest.raises(ValueError, match="character_count"):
        replace(_large_value(), character_count=-1)


def test_large_value_owns_nested_models_and_helpers_revalidate_tampering() -> None:
    summary = Base64Summary(1, "0" * 64, b"x")
    value = _large_value(media=summary)
    object.__setattr__(summary, "byte_length", -1)
    assert value.media is not None
    assert value.media.byte_length == 1

    object.__setattr__(value, "character_count", -1)
    with pytest.raises(ValueError, match="character_count"):
        value_length(value)
    with pytest.raises(ValueError, match="character_count"):
        value_prefix(value, 1)


@pytest.mark.parametrize("count", [-1, True, 10**MAX_MODEL_INTEGER_DIGITS])
def test_value_prefix_rejects_invalid_counts(count: object) -> None:
    with pytest.raises(ValueError, match="count"):
        value_prefix("text", count)  # type: ignore[arg-type]


def test_text_part_requires_materialized_text() -> None:
    with pytest.raises(ValueError, match="requires text"):
        _part(text=None)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("parts", ("not-a-part",), "NormalizedPart"),
        ("parts", 3, "sequence"),
        ("parts", (), "at least one"),
        ("inline_bytes", True, "inline_bytes"),
        ("inline_bytes", -1, "inline_bytes"),
        ("inline_bytes", 10**MAX_MODEL_INTEGER_DIGITS, "inline_bytes"),
        ("fingerprint", 3, "fingerprint"),
        ("fingerprint", "sha256:x", "fingerprint"),
        ("fingerprint", "bad\ud800value", "surrogate"),
        ("schema_version", "2.0", "schema_version"),
        ("schema_version", None, "schema_version"),
    ],
)
def test_manifest_rejects_invalid_runtime_values(field: str, value: object, message: str) -> None:
    generated = normalize([{"type": "text", "text": "valid"}])
    with pytest.raises(ValueError, match=message):
        replace(generated, **{field: value})


def test_manifest_freezes_a_sequence_of_parts_as_a_tuple() -> None:
    generated = normalize([{"type": "text", "text": ""}])
    manifest = Manifest(
        parts=list(generated.parts),
        fingerprint=generated.fingerprint,
        inline_bytes=generated.inline_bytes,
    )  # type: ignore[arg-type]
    assert manifest.parts == generated.parts


def test_manifest_iteration_does_not_trust_a_forged_sequence_length() -> None:
    class ForgedLengthParts(Sequence[NormalizedPart]):
        def __init__(self, parts: tuple[NormalizedPart, ...]) -> None:
            self.parts = parts

        def __getitem__(self, index: int) -> NormalizedPart:
            return self.parts[index]

        def __len__(self) -> int:
            return 1

        def __iter__(self) -> Iterator[NormalizedPart]:
            return iter(self.parts)

    generated = normalize(
        [
            {"type": "text", "text": "one"},
            {"type": "text", "text": "two"},
        ]
    )
    manifest = Manifest(
        parts=ForgedLengthParts(generated.parts),  # type: ignore[arg-type]
        fingerprint=generated.fingerprint,
        inline_bytes=generated.inline_bytes,
    )

    assert manifest.part_count == 2


def test_manifest_iteration_stops_at_the_public_part_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EndlessParts(Sequence[NormalizedPart]):
        def __init__(self, part: NormalizedPart) -> None:
            self.part = part
            self.items_requested = 0

        def __getitem__(self, index: int) -> NormalizedPart:
            return self.part

        def __len__(self) -> int:
            return 0

        def __iter__(self) -> Iterator[NormalizedPart]:
            while True:
                self.items_requested += 1
                yield self.part

    generated = normalize([{"type": "text", "text": "one"}])
    parts = EndlessParts(generated.parts[0])
    monkeypatch.setattr(model_module, "MAX_POLICY_PARTS", 2)

    with pytest.raises(ValueError, match="at most 2"):
        Manifest(parts, generated.fingerprint, generated.inline_bytes)  # type: ignore[arg-type]
    assert parts.items_requested == 3


def test_manifest_owns_deep_part_snapshots() -> None:
    generated = normalize([{"type": "text", "text": "original"}])
    caller_part = generated.parts[0]
    manifest = Manifest(
        parts=(caller_part,),
        fingerprint=generated.fingerprint,
        inline_bytes=generated.inline_bytes,
    )
    object.__setattr__(caller_part, "text", "mutated!")

    assert manifest.to_dict()["parts"][0]["text"] == "original"


def test_serialization_revalidates_object_setattr_tampering() -> None:
    standalone = normalize([{"type": "text", "text": "first"}]).parts[0]
    object.__setattr__(standalone, "text", "other")
    with pytest.raises(ValueError, match="fingerprint"):
        standalone.to_dict()

    manifest = normalize([{"type": "text", "text": "manifest"}])
    object.__setattr__(manifest.parts[0], "ordinal", 9)
    with pytest.raises(ValueError, match="ordinals"):
        manifest.to_dict()


def test_manifest_rejects_cross_field_inconsistency() -> None:
    generated = normalize(
        [
            {"type": "text", "text": "one"},
            {"type": "text", "text": "two"},
        ]
    )
    with pytest.raises(ValueError, match="ordinals"):
        Manifest(
            parts=(generated.parts[1], generated.parts[0]),
            fingerprint=generated.fingerprint,
            inline_bytes=generated.inline_bytes,
        )
    with pytest.raises(ValueError, match="inline_bytes"):
        replace(generated, inline_bytes=generated.inline_bytes + 1)
    with pytest.raises(ValueError, match="fingerprint"):
        replace(generated, fingerprint="sha256:" + "f" * 64)
    with pytest.raises(ValueError, match="schema_version"):
        replace(generated, schema_version="banana")


def test_model_integer_boundary_serializes_at_cpython_minimum_limit() -> None:
    previous_limit = sys.get_int_max_str_digits()
    boundary = (10**MAX_MODEL_INTEGER_DIGITS) - 1
    try:
        sys.set_int_max_str_digits(MAX_MODEL_INTEGER_DIGITS)
        issue = DeferredIssue("too_large", "measured value was refused")
        value = LargeValue(
            character_count=boundary,
            prefix="A" * LARGE_VALUE_PREFIX_CHARACTERS,
            media_issue=issue,
        )
        encoded = json.dumps({"character_count": value.character_count}, allow_nan=False)
        assert json.loads(encoded)["character_count"] == boundary
    finally:
        sys.set_int_max_str_digits(previous_limit)
