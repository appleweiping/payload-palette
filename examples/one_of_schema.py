"""Offline exact-one schema import, overlap rejection and export roundtrip."""

from __future__ import annotations

import json

from payload_palette import export_output_schema, load_output_schema


def main() -> None:
    document = {
        "$defs": {"whole": {"type": "integer"}},
        "oneOf": [
            {"type": "number"},
            {"$ref": "#/$defs/whole"},
        ],
    }
    schema = load_output_schema(document)
    exported = export_output_schema(schema)
    restored = load_output_schema(exported)
    for candidate, accepted in ((1.5, True), (1, False), (1.0, False), ("1", False)):
        if (schema.validate(candidate) == ()) is not accepted:
            raise RuntimeError("exact-one acceptance changed")
        if schema.validate(candidate) != restored.validate(candidate):
            raise RuntimeError("exact-one export changed acceptance")
    if "oneOf" not in exported or "anyOf" in exported:
        raise RuntimeError("exact-one keyword was not preserved")
    print(json.dumps({"accepted": 1, "rejected": 3, "roundtrip": True}, sort_keys=True))


if __name__ == "__main__":
    main()
