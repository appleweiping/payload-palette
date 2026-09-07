"""Compile trusted runtime annotations, exchange schema data and validate JSON."""

from __future__ import annotations

import json
from typing import Annotated, Literal, NotRequired, TypedDict

from payload_palette import (
    AnnotationAdapter,
    FieldConstraints,
    JSONValue,
    export_output_schema,
    load_output_schema,
)


def run_example() -> dict[str, JSONValue]:
    # Explicit annotation objects also work in a module using postponed annotations.
    classification = TypedDict(  # noqa: UP013 -- preserve explicit objects with postponed annotations
        "Classification",
        {
            "label": Annotated[str, FieldConstraints(min_length=1, max_length=120)],
            "confidence": Annotated[float, FieldConstraints(minimum=0, maximum=1)] | None,
            "tags": NotRequired[list[Literal["vision", "audio"]]],
        },
    )
    adapter = AnnotationAdapter(classification)
    contract = export_output_schema(adapter.schema)
    imported = load_output_schema(contract)
    output = adapter.validate_json_bytes(b'{"label":"bicycle","confidence":null,"tags":["vision"]}')
    if imported.validate(output):
        raise AssertionError("export/import changed the output contract")
    return {
        "schema": contract,
        "output": output,
        "serialized": adapter.dump_json(output).decode("utf-8"),
    }


if __name__ == "__main__":
    print(json.dumps(run_example(), indent=2))
