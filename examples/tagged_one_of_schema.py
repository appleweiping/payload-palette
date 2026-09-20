"""Offline tagged-object union with local definitions and portable roundtrip."""

from __future__ import annotations

import json

from payload_palette import export_output_schema, load_output_schema


def main() -> None:
    document = {
        "$defs": {
            "Reading": {
                "type": "object",
                "title": "Reading",
                "properties": {
                    "kind": {"type": "string", "const": "reading", "title": "Kind"},
                    "watts": {"type": "number", "minimum": 0},
                },
                "required": ["kind", "watts"],
            },
            "Alert": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "const": "alert"},
                    "message": {"type": "string", "minLength": 1},
                },
                "required": ["kind", "message"],
            },
        },
        "oneOf": [{"$ref": "#/$defs/Reading"}, {"$ref": "#/$defs/Alert"}],
        "discriminator": {
            "propertyName": "kind",
            "mapping": {"reading": "#/$defs/Reading", "alert": "#/$defs/Alert"},
        },
    }
    schema = load_output_schema(document)
    restored = load_output_schema(export_output_schema(schema))
    candidates = (
        ({"kind": "reading", "watts": 25}, True),
        ({"kind": "alert", "message": "high"}, True),
        ({"kind": "reading", "watts": -1}, False),
        ({"kind": "secret", "message": "hidden"}, False),
    )
    for value, expected in candidates:
        if (schema.validate(value) == ()) is not expected:
            raise RuntimeError("tagged acceptance changed")
        if (restored.validate(value) == ()) is not expected:
            raise RuntimeError("portable schema acceptance changed")
    print(json.dumps({"accepted": 2, "rejected": 2, "roundtrip": True}, sort_keys=True))


if __name__ == "__main__":
    main()
