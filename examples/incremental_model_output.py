"""Offline generated-output chunks: syntax progress is not semantic approval."""

from __future__ import annotations

import json

from payload_palette import (
    IncrementalProgress,
    OutputSchema,
    RuleBinding,
    StringChoices,
    TrimmedString,
    ValidationPipeline,
    validate_output_chunks,
)


def main() -> None:
    schema = OutputSchema(
        "object",
        properties={"label": OutputSchema("string"), "caption": OutputSchema("string")},
        required=("label", "caption"),
    )
    pipeline = ValidationPipeline(
        schema,
        (
            RuleBinding("trim", ("label",), TrimmedString(), "fix"),
            RuleBinding("choice", ("label",), StringChoices(("cat", "dog"), fix_case=True), "fix"),
        ),
    )
    source_closed = []
    event_count = 0

    def local_model_chunks():
        # Intentionally split raw UTF-8 and escaped surrogate sequences across
        # provider-style chunks. This is an offline source, not a live adapter.
        raw = '{"label":" CAT ","caption":"猫 \\ud83d\\ude00"}'.encode()
        try:
            for offset in range(0, len(raw), 2):
                yield raw[offset : offset + 2]
        finally:
            source_closed.append(True)

    def observe(progress: IncrementalProgress) -> None:
        nonlocal event_count
        # These events contain actual values/keys if accessed. Do not publish
        # them as validated output or add them to privacy-minimized history.
        event_count += len(progress.events)

    result = validate_output_chunks(
        local_model_chunks(),
        pipeline,
        on_progress=observe,
        close_iterator=True,
    )
    print(
        json.dumps(
            {
                "source_closed": bool(source_closed),
                "syntax_events_before_finish": event_count,
                "report": result.report.to_dict(include_output=True),
            },
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
