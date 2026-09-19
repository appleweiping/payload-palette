"""Offline local-definition schema reuse without fetching or evaluating code."""

from __future__ import annotations

import json

from payload_palette import export_output_schema, load_output_schema


def main() -> None:
    document = {
        "$defs": {
            "score": {"type": "integer", "minimum": 0, "maximum": 10},
            "rating": {
                "type": "object",
                "properties": {"score": {"$ref": "#/$defs/score"}},
                "required": ["score"],
                "additionalProperties": False,
            },
        },
        "type": "object",
        "properties": {
            "first": {"$ref": "#/$defs/rating"},
            "second": {"$ref": "#/$defs/rating"},
        },
        "required": ["first", "second"],
        "additionalProperties": False,
    }
    schema = load_output_schema(document)
    accepted = {"first": {"score": 8}, "second": {"score": 9}}
    rejected = {"first": {"score": -1}, "second": {"score": 9}}
    if schema.validate(accepted) or not schema.validate(rejected):
        raise RuntimeError("local definition contract differs from expected values")
    expanded = export_output_schema(schema)
    if "$defs" in expanded or load_output_schema(expanded).validate(accepted):
        raise RuntimeError("expanded exported schema changed the accepted contract")
    print(json.dumps({"accepted": True, "rejected": True, "expanded_export": True}, sort_keys=True))


if __name__ == "__main__":
    main()
