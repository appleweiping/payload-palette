"""Offline bounded allOf intersection, local-reference and closed-object example."""

from __future__ import annotations

import json

from payload_palette import export_output_schema, load_output_schema


def main() -> None:
    document = {
        "$defs": {"minimum": {"type": "number", "minimum": 2}},
        "allOf": [
            {"$ref": "#/$defs/minimum"},
            {"type": "number", "maximum": 5},
        ],
    }
    schema = load_output_schema(document)
    restored = load_output_schema(export_output_schema(schema))
    for candidate, accepted in ((1, False), (2, True), (3.5, True), (6, False)):
        if (not schema.validate(candidate)) is not accepted:
            raise RuntimeError("intersection acceptance changed")
        if schema.validate(candidate) != restored.validate(candidate):
            raise RuntimeError("intersection export changed acceptance")

    closed = load_output_schema(
        {
            "allOf": [
                {
                    "type": "object",
                    "properties": {"a": {"type": "integer"}},
                    "required": ["a"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {"b": {"type": "integer"}},
                    "required": ["b"],
                    "additionalProperties": False,
                },
            ]
        }
    )
    if [issue.code for issue in closed.validate({"a": 1, "b": 2})] != ["schema_all_of"]:
        raise RuntimeError("closed allOf objects were incorrectly merged")
    print(
        json.dumps(
            {"accepted": 2, "rejected": 2, "roundtrip": True, "closed_objects_not_merged": True},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
