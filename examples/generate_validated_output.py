"""Exercise a complete re-ask lifecycle with an asynchronous offline adapter."""

from __future__ import annotations

import asyncio
import json

from payload_palette import (
    AsyncGenerationRunner,
    GeneratedResponse,
    GenerationPolicy,
    GenerationRequest,
    JSONValue,
    OutputSchema,
    RuleBinding,
    StringChoices,
    TokenUsage,
    ValidationPipeline,
)


class OfflineClassifier:
    """Two deterministic responses demonstrate rejection followed by regeneration."""

    async def generate(self, request: GenerationRequest) -> GeneratedResponse:
        await asyncio.sleep(0)
        if not request.feedback:
            return GeneratedResponse('{"category":"unsure"}', TokenUsage(20, 5))
        return GeneratedResponse('{"category":"image"}', TokenUsage(25, 5))


async def run_example() -> dict[str, JSONValue]:
    pipeline = ValidationPipeline(
        OutputSchema(
            "object", properties={"category": OutputSchema("string")}, required=("category",)
        ),
        (RuleBinding("category", ("category",), StringChoices(("image", "audio", "video"))),),
    )
    runner = AsyncGenerationRunner(
        pipeline,
        OfflineClassifier(),
        GenerationPolicy(max_attempts=2, max_total_tokens=100),
    )
    return (await runner.run("Classify a photograph of a bicycle.")).to_dict(include_output=True)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_example()), indent=2))
