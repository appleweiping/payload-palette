from __future__ import annotations

import pytest

from payload_palette.errors import PayloadValidationError
from payload_palette.models import MAX_MODEL_INTEGER_DIGITS
from payload_palette.parser import extract_entries, parse_document, parse_part


def test_direct_array_paths_preserve_order() -> None:
    entries = extract_entries([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert [path for _, path in entries] == ["$[0]", "$[1]"]


def test_content_envelope() -> None:
    specs = parse_document({"content": [{"type": "text", "text": "hello"}]}, 10)
    assert specs[0].path == "$.content[0]"


def test_messages_are_flattened_in_request_order() -> None:
    document = {
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": [{"type": "text", "text": "question"}]},
        ]
    }
    specs = parse_document(document, 10)
    assert [spec.value for spec in specs] == ["rules", "question"]
    assert [spec.ordinal for spec in specs] == [0, 1]


def test_single_typed_part() -> None:
    spec = parse_document({"type": "text", "text": "hello"}, 2)[0]
    assert spec.path == "$"


@pytest.mark.parametrize("document", [None, 1, "text"])
def test_invalid_root_type(document: object) -> None:
    with pytest.raises(PayloadValidationError, match="payload root"):
        extract_entries(document)


def test_unknown_envelope_shape() -> None:
    with pytest.raises(PayloadValidationError) as error:
        extract_entries({"model": "example"})
    assert error.value.issues[0].code == "payload_shape"


def test_conflicting_envelope_shapes_are_rejected() -> None:
    with pytest.raises(PayloadValidationError) as error:
        extract_entries({"messages": [], "content": []})
    assert error.value.issues[0].code == "ambiguous_envelope"


def test_messages_must_be_array() -> None:
    with pytest.raises(PayloadValidationError) as error:
        extract_entries({"messages": {}})
    assert error.value.issues[0].path == "$.messages"


def test_message_shape_errors_are_aggregated() -> None:
    with pytest.raises(PayloadValidationError) as error:
        extract_entries({"messages": [3, {"content": None}]})
    assert [issue.code for issue in error.value.issues] == ["message_type", "content_type"]


def test_empty_content_rejected() -> None:
    with pytest.raises(PayloadValidationError, match="does not contain"):
        extract_entries({"content": []})


def test_max_parts_enforced() -> None:
    parts = [{"type": "text", "text": str(index)} for index in range(3)]
    with pytest.raises(PayloadValidationError) as error:
        parse_document(parts, 2)
    assert error.value.issues[0].code == "too_many_parts"


def test_structural_error_aggregation_is_bounded_by_part_limit() -> None:
    document = {"messages": [None, None, None]}
    with pytest.raises(PayloadValidationError) as error:
        parse_document(document, 2)
    assert error.value.issues[0].code == "too_many_issues"


def test_part_shape_errors_are_aggregated() -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_document([3, {}, {"type": "mystery"}], 10)
    assert [issue.code for issue in error.value.issues] == [
        "part_type",
        "missing_type",
        "unknown_type",
    ]


def test_text_requires_string() -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part({"type": "text", "text": 42}, "$[0]", 0)
    assert error.value.issues[0].path == "$[0].text"


@pytest.mark.parametrize("ordinal", [True, -1, 10**MAX_MODEL_INTEGER_DIGITS])
def test_part_spec_rejects_non_serializable_ordinals(ordinal: object) -> None:
    with pytest.raises(ValueError, match="ordinal"):
        parse_part({"type": "text", "text": "ok"}, "$[0]", ordinal)  # type: ignore[arg-type]


def test_bare_image_requires_mime(png_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part({"type": "image", "data": png_base64}, "$[0]", 0)
    assert error.value.issues[0].code == "missing_mime"


def test_input_audio_format_infers_mime(wav_base64: str) -> None:
    part = parse_part(
        {"type": "input_audio", "input_audio": {"data": wav_base64, "format": "wav"}},
        "$[0]",
        0,
    )
    assert part.declared_mime_type == "audio/wav"
    assert part.source_hint == "inline"


def test_input_image_accepts_image_url(png_data_url: str) -> None:
    part = parse_part(
        {"type": "input_image", "image_url": png_data_url},
        "$[0]",
        0,
    )
    assert part.kind == "image"
    assert part.value == png_data_url


def test_media_container_must_be_string_or_object() -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part({"type": "input_audio", "input_audio": 42}, "$[0]", 0)
    assert error.value.issues[0].code == "media_container"


def test_container_format_requires_string(wav_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part(
            {"type": "input_audio", "input_audio": {"data": wav_base64, "format": 42}},
            "$[0]",
            0,
        )
    assert error.value.issues[0].code == "format_type"


def test_conflicting_nested_mime_is_rejected(wav_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part(
            {
                "type": "input_audio",
                "mime_type": "audio/mpeg",
                "input_audio": {
                    "data": wav_base64,
                    "media_type": "audio/wav",
                },
            },
            "$[0]",
            0,
        )
    assert error.value.issues[0].code == "mime_conflict"


def test_conflicting_mime_aliases_in_one_object_are_rejected(wav_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part(
            {
                "type": "audio",
                "data": wav_base64,
                "mime_type": "audio/wav",
                "media_type": "audio/mpeg",
            },
            "$[0]",
            0,
        )
    assert error.value.issues[0].code == "mime_conflict"


def test_conflicting_format_and_mime_is_rejected(wav_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part(
            {
                "type": "audio",
                "data": wav_base64,
                "mime_type": "audio/mpeg",
                "format": "wav",
            },
            "$[0]",
            0,
        )
    assert error.value.issues[0].code == "mime_conflict"


def test_generic_format_requires_string(png_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part({"type": "image", "data": png_base64, "format": 42}, "$[0]", 0)
    assert error.value.issues[0].code == "format_type"


def test_mime_field_requires_string(png_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part(
            {"type": "image", "data": png_base64, "mime_type": 42},
            "$[0]",
            0,
        )
    assert error.value.issues[0].code == "mime_type_field"


def test_missing_media_value() -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part({"type": "video", "mime_type": "video/mp4"}, "$[0]", 0)
    assert error.value.issues[0].code == "missing_media"


def test_multiple_outer_media_values_are_rejected(png_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part(
            {
                "type": "image",
                "data": png_base64,
                "image_base64": png_base64,
                "mime_type": "image/png",
            },
            "$[0]",
            0,
        )
    assert error.value.issues[0].code == "ambiguous_media"


def test_container_data_and_url_are_rejected_as_ambiguous(png_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part(
            {
                "type": "image_url",
                "image_url": {"data": png_base64, "url": "https://cdn.example/a"},
            },
            "$[0]",
            0,
        )
    assert error.value.issues[0].code == "ambiguous_media"


def test_container_and_outer_value_are_rejected_as_ambiguous(png_data_url: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part(
            {
                "type": "image_url",
                "image_url": png_data_url,
                "data": "AA==",
            },
            "$[0]",
            0,
        )
    assert error.value.issues[0].code == "ambiguous_media"


def test_invalid_url_scheme_is_not_treated_as_base64() -> None:
    with pytest.raises(PayloadValidationError) as error:
        parse_part(
            {"type": "image_url", "image_url": {"url": "file:///etc/passwd"}},
            "$[0]",
            0,
        )
    assert error.value.issues[0].code == "url_scheme"


def test_safe_attributes_are_retained(png_data_url: str) -> None:
    part = parse_part(
        {
            "type": "image_url",
            "image_url": {"url": png_data_url},
            "detail": "high",
            "ignored": "not copied",
        },
        "$[0]",
        0,
    )
    assert part.attributes == {"detail": "high"}
