"""Explicit offline SSE-to-JSON application protocol, not a provider adapter."""

from __future__ import annotations

import json

from payload_palette import IncrementalOutputSession, OutputSchema, SSEDecoder, ValidationPipeline


def run_example() -> dict[str, object]:
    expected = {"label": "雪🙂", "score": 2}
    # This example's application contract: `json-fragment` carries contiguous
    # JSON text; one `document-complete` must follow it, with no later messages.
    # SSE itself supplies neither meaning. Blank-line framing is not JSON EOF.
    wire = (
        'event: json-fragment\ndata: {"label":\n\n'
        'event: json-fragment\ndata: "雪🙂",\ndata: "score":2}\n\n'
        "event: document-complete\ndata: complete\n\n"
    ).encode()
    framing = SSEDecoder(utf8_errors="strict")
    document = IncrementalOutputSession(
        ValidationPipeline(
            OutputSchema(
                "object",
                properties={"label": OutputSchema("string"), "score": OutputSchema("integer")},
                required=("label", "score"),
            )
        )
    )
    completed = False
    try:
        for position in range(0, len(wire), 3):
            for event in framing.feed(wire[position : position + 3]).events:
                if completed:
                    raise ValueError("application message after completion")
                if event.event_type == "json-fragment":
                    document.feed(event.data.encode("utf-8"))
                elif event.event_type == "document-complete" and event.data == "complete":
                    completed = True
                else:
                    raise ValueError("unexpected application event")
        final = framing.finish().snapshot
        if final.discarded_pending_block or final.discarded_unterminated_line or not completed:
            raise ValueError("incomplete application stream; semantic approval was not run")
        # Local bytes have actually ended and our explicit completion contract
        # succeeded. No iterator/connection is owned by this demonstration.
        result = document.finish()
        if not result.report.valid or result.report.output != expected:
            raise AssertionError("SSE JSON demonstration failed")
        return {"valid": True, "messages": final.events, "output": result.report.output}
    finally:
        document.close()
        framing.close()


if __name__ == "__main__":
    print(json.dumps(run_example(), ensure_ascii=True, sort_keys=True))
