"""A provider-independent output contract; run without credentials or network."""

from __future__ import annotations

import json

from payload_palette import (
    JSONValue,
    OutputSchema,
    RuleBinding,
    StringChoices,
    TrimmedString,
    ValidationPipeline,
)


def run_example() -> dict[str, JSONValue]:
    schema = OutputSchema(
        "object",
        properties={
            "summary": OutputSchema("string", min_length=1, max_length=280),
            "confidence": OutputSchema("number", minimum=0, maximum=1),
            "category": OutputSchema("string"),
            "unverified_note": OutputSchema("string"),
        },
        required=("summary", "confidence", "category"),
    )
    pipeline = ValidationPipeline(
        schema,
        rules=(
            RuleBinding("trim_summary", ("summary",), TrimmedString(), "fix"),
            RuleBinding(
                "known_category",
                ("category",),
                StringChoices(("image", "audio", "video"), fix_case=True),
                "fix",
            ),
            RuleBinding(
                "verified_note", ("unverified_note",), StringChoices(("verified",)), "filter"
            ),
        ),
    )
    # Replace these bytes with a provider's complete generated JSON response.
    report = pipeline.validate_json_bytes(
        b'{"summary":" A red bicycle. ","confidence":0.8,"category":"IMAGE",'
        b'"unverified_note":"maybe manufactured in 2020"}'
    )
    return report.to_dict(include_output=True)


if __name__ == "__main__":
    print(json.dumps(run_example(), indent=2))
