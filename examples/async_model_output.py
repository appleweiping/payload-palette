"""Offline asynchronous chunk source, backpressure and final validation."""

from __future__ import annotations

import asyncio
import json

from payload_palette import (
    AsyncOutputPolicy,
    IncrementalProgress,
    OutputSchema,
    ValidationPipeline,
    validate_async_output_chunks,
)


async def run_example() -> dict[str, object]:
    expected = {"label": "星空𝄞", "confidence": 0.9}
    wire = json.dumps(expected, ensure_ascii=False).encode("utf-8")
    state = {"closed": False, "observations": 0, "values": 0}

    async def source():
        try:
            for position in range(0, len(wire), 3):
                await asyncio.sleep(0)  # Cooperative local source, not a model/network request.
                yield wire[position : position + 3]
        finally:
            state["closed"] = True

    async def observe(progress: IncrementalProgress) -> None:
        state["observations"] += 1
        state["values"] += len(progress.events)
        await asyncio.sleep(0)  # The next pull waits for this observer.

    result = await validate_async_output_chunks(
        source(),
        ValidationPipeline(
            OutputSchema(
                "object",
                properties={"label": OutputSchema("string"), "confidence": OutputSchema("number")},
                required=("label", "confidence"),
            )
        ),
        policy=AsyncOutputPolicy(timeout_seconds=10, cleanup_timeout_seconds=2),
        on_progress=observe,
        close_iterator=True,
    )
    if not result.report.valid or result.report.output != expected or not state["closed"]:
        raise AssertionError("asynchronous output example did not validate or close its source")
    return {"valid": True, "output": result.report.output, **state}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_example()), ensure_ascii=True, sort_keys=True))
