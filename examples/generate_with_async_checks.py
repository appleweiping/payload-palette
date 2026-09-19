"""Offline repair, parallel semantic checks, and bounded regeneration; no network."""

from __future__ import annotations

import asyncio
import json

from payload_palette import (
    AsyncGenerationRunner,
    AsyncRuleBinding,
    AsyncValidationPipeline,
    GeneratedResponse,
    GenerationPolicy,
    GenerationRequest,
    JSONValue,
    OutputSchema,
    RuleBinding,
    RuleContext,
    RuleResult,
    TokenUsage,
    TrimmedString,
    ValidationPipeline,
)


class OfflineProvider:
    async def generate(self, request: GenerationRequest) -> GeneratedResponse:
        await asyncio.sleep(0)
        label = " draft " if not request.feedback else " ready "
        return GeneratedResponse(json.dumps({"label": label}), TokenUsage(8, 3))


class CatalogCheck:
    """An awaited local lookup stands in for a caller-owned asynchronous service."""

    async def check(self, value: JSONValue, context: RuleContext) -> RuleResult:
        await asyncio.sleep(0)
        return (
            RuleResult(True)
            if value == "ready"
            else RuleResult(False, "not_in_catalog", "label is not in the offline catalog")
        )


class FinalShapeCheck:
    async def check(self, value: JSONValue, context: RuleContext) -> RuleResult:
        await asyncio.sleep(0)
        return (
            RuleResult(True)
            if isinstance(value, str) and value == value.strip()
            else RuleResult(False, "not_normalized", "expected a normalized final label")
        )


async def run_example() -> dict[str, JSONValue]:
    synchronous = ValidationPipeline(
        OutputSchema("object", properties={"label": OutputSchema("string")}, required=("label",)),
        (RuleBinding("trim", ("label",), TrimmedString(), "fix"),),
    )
    pipeline = AsyncValidationPipeline(
        synchronous,
        (
            AsyncRuleBinding("catalog", ("label",), CatalogCheck()),
            AsyncRuleBinding("shape", ("label",), FinalShapeCheck()),
        ),
    )
    report = await AsyncGenerationRunner(
        pipeline, OfflineProvider(), GenerationPolicy(max_attempts=2, max_validator_invocations=10)
    ).run("Choose a label from the offline catalog.")
    if not report.valid or report.output != {"label": "ready"}:
        raise RuntimeError("the offline generation did not produce the validated final object")
    if report.validator_invocations != 10 or len(report.attempts) != 2:
        raise RuntimeError(
            "repair and asynchronous checks did not share the expected lifetime budget"
        )
    if [item.status for item in report.attempts] != ["invalid_output", "accepted"]:
        raise RuntimeError("the expected complete-output re-ask did not occur")
    return report.to_dict(include_output=True)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_example()), indent=2))
