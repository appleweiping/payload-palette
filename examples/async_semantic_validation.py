"""Offline final semantic checks over a repaired, schema-valid classification."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from payload_palette import (
    AsyncRuleBinding,
    AsyncValidationPipeline,
    AsyncValidationPolicy,
    JSONValue,
    OutputSchema,
    RuleBinding,
    RuleContext,
    RuleResult,
    TrimmedString,
    ValidationPipeline,
)


@dataclass(frozen=True)
class LocalCatalog:
    allowed: frozenset[str]

    async def check(self, value: JSONValue, context: RuleContext) -> RuleResult:
        # A real cooperative suspension, deliberately backed only by local data.
        # This is not an HTTP client or a claim about remote lookup latency.
        await asyncio.sleep(0)
        if isinstance(value, str) and value in self.allowed:
            return RuleResult(True)
        return RuleResult(False, "unknown_label", "label is absent from the local catalog")


async def run_example() -> dict[str, JSONValue]:
    schema = OutputSchema(
        "object",
        properties={"category": OutputSchema("string"), "visibility": OutputSchema("string")},
        required=("category", "visibility"),
    )
    pipeline = AsyncValidationPipeline(
        ValidationPipeline(
            schema,
            (RuleBinding("trim_category", ("category",), TrimmedString(), "fix"),),
        ),
        (
            AsyncRuleBinding(
                "category_catalog", ("category",), LocalCatalog(frozenset({"diagram", "photo"}))
            ),
            AsyncRuleBinding(
                "visibility_catalog",
                ("visibility",),
                LocalCatalog(frozenset({"public", "internal"})),
            ),
        ),
        AsyncValidationPolicy(max_concurrency=2),
    )
    approved = await pipeline.validate({"category": " diagram ", "visibility": "internal"})
    rejected = await pipeline.validate({"category": "photo", "visibility": "unknown"})
    assert approved.valid and not rejected.valid
    assert approved.output == {"category": "diagram", "visibility": "internal"}
    assert rejected.output is None
    return {"approved": approved.to_dict(include_output=True), "rejected": rejected.to_dict()}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_example()), ensure_ascii=False, indent=2))
