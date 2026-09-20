"""Optional independent Draft 2020-12 oracle; run with jsonschema installed."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from payload_palette import export_output_schema, load_output_schema


def test_draft_2020_12_differential_corpus_has_positive_and_negative_cases() -> None:
    corpus: list[tuple[dict[str, Any], list[Any]]] = [
        (
            {"oneOf": [{"type": "integer"}, {"type": "string", "minLength": 2}]},
            [1, 1.0, "ok", "x", None, True],
        ),
        (
            {"oneOf": [{"type": "number"}, {"type": "integer"}]},
            [1, 1.0, 1.5, -2, -2.5, "1", True],
        ),
        (
            {
                "oneOf": [
                    {"type": "object", "required": ["a"], "properties": {"a": {"type": "integer"}}},
                    {"type": "object", "required": ["b"], "properties": {"b": {"type": "string"}}},
                ]
            },
            [{"a": 1}, {"b": "x"}, {"a": 1, "b": "x"}, {}, {"a": "bad"}],
        ),
        (
            {
                "$defs": {
                    "num": {"type": "number", "minimum": 0},
                    "label": {"type": "string", "minLength": 1},
                },
                "oneOf": [{"$ref": "#/$defs/num"}, {"$ref": "#/$defs/label"}],
            },
            [0, 0.0, -1, "x", "", None],
        ),
    ]
    accepted = rejected = 0
    for document, values in corpus:
        Draft202012Validator.check_schema(document)
        oracle = Draft202012Validator(document)
        schema = load_output_schema(document)
        assert schema.kind == "one_of"
        assert "oneOf" in export_output_schema(schema)
        for value in values:
            expected = oracle.is_valid(value)
            actual = schema.validate(value) == ()
            assert actual is expected, (document, value)
            accepted += expected
            rejected += not expected
    assert (accepted, rejected) == (10, 14)
