"""Complete deterministic transcripts captured before the private async extraction.

Expected bytes were produced at signed e8733364, not by the new integration.
Real timers/cancellation use separate event-controlled tests, not this corpus.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from payload_palette import (
    AsyncGenerationRunner,
    AsyncRuleBinding,
    AsyncValidationPipeline,
    AsyncValidationPolicy,
    GeneratedResponse,
    GenerationPolicy,
    OutputLimits,
    OutputSchema,
    RuleBinding,
    RuleResult,
    TokenUsage,
    TrimmedString,
    ValidationPipeline,
)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def capture_signed_behavior():
    transcripts = []

    def pipeline(case, calls):
        class Check:
            def check(self, value, context):
                calls.append([context.rule_id, context.phase, value])
                if case in {"repair", "repair-budget"}:
                    return TrimmedString().check(value, context)
                if case == "contract":
                    raise ValueError("private validator detail")
                if case == "malformed":
                    return None
                if case == "invalid-fix":
                    return RuleResult(False, "bad", "private", object())
                if case == "filter":
                    return RuleResult(False, "remove", "private")
                return (
                    RuleResult(True)
                    if value == "yes"
                    else RuleResult(False, "validator_budget", "private")
                )

        if case in {
            "empty",
            "schema",
            "ingress",
            "unicode",
            "provider",
            "tokens",
            "bytes",
            "cumulative-bytes",
        }:
            return ValidationPipeline(OutputSchema("string"))
        if case in {"optional", "filter"}:
            return ValidationPipeline(
                OutputSchema("object", properties={"x": OutputSchema("string")}),
                (
                    RuleBinding(
                        "check", ("x",), Check(), "filter" if case == "filter" else "reject"
                    ),
                ),
            )
        return ValidationPipeline(
            OutputSchema("string"),
            (
                RuleBinding(
                    "check",
                    (),
                    Check(),
                    "fix" if case in {"repair", "repair-budget", "invalid-fix"} else "reject",
                ),
            ),
        )

    specs = [
        ("empty", ['"yes"'], {}),
        ("schema", ["1", '"yes"'], {}),
        ("ingress", ["{", '"yes"'], {}),
        ("unicode", ['"电力😀"'], {}),
        ("ordinary", ['"no"', '"yes"'], {}),
        ("ordinary", ['"no"'] * 3, {}),
        ("ordinary", ['"no"'], {"max_validator_invocations": 1}),
        ("repair", ['" yes "'], {}),
        ("repair-budget", ['" yes "'], {"max_validator_invocations": 1}),
        ("repair-budget", ['" yes "'], {"max_validator_invocations": 2}),
        ("repair-budget", ['" yes "'], {"max_validator_invocations": 3}),
        ("contract", ['"yes"'], {}),
        ("malformed", ['"yes"'], {}),
        ("invalid-fix", ['"yes"'], {}),
        ("optional", ["{}"], {}),
        ("filter", ['{"x":"no"}'], {}),
        ("provider", [ValueError("provider detail")], {}),
        ("provider", [None], {}),
        ("tokens", ['"yes"'], {"max_total_tokens": 2}),
        ("bytes", ['"yes"'], {"max_response_bytes": 2}),
        ("cumulative-bytes", ["1", "1", '"yes"'], {"max_total_response_bytes": 2}),
    ]
    for number, (case, responses, policy) in enumerate(specs):
        calls, requests = [], []

        class Provider:
            def __init__(self, requested, scripted):
                self.requests, self.responses = requested, scripted

            async def generate(self, request):
                self.requests.append(asdict(request))
                await asyncio.sleep(0)
                value = self.responses[len(self.requests) - 1]
                if isinstance(value, Exception):
                    raise value
                return GeneratedResponse(value, TokenUsage(2, 1)) if type(value) is str else value

        report = asyncio.run(
            AsyncGenerationRunner(
                pipeline(case, calls), Provider(requests, responses), GenerationPolicy(**policy)
            ).run("offline baseline")
        )
        transcripts.append(
            {
                "case": f"generation-{number}-{case}",
                "requests": requests,
                "calls": calls,
                "report": report.to_dict(include_output=True),
            }
        )

    for case in (
        "accept",
        "reject",
        "reserved-code",
        "contract",
        "malformed",
        "repair",
        "optional",
        "base-budget",
        "schema",
        "budget",
    ):
        for max_issues in (1, 3):
            calls = []

            class AsyncCheck:
                def __init__(self, index, events, behavior):
                    self.index, self.calls, self.case = index, events, behavior

                async def check(self, value, context):
                    self.calls.append([self.index, "entry", context.phase, value])
                    await asyncio.sleep(0)
                    self.calls.append([self.index, "exit"])
                    if self.case == "contract":
                        raise ValueError("private")
                    if self.case == "malformed":
                        return object()
                    if self.case in {"reject", "reserved-code"}:
                        return RuleResult(
                            False,
                            "validation_deadline" if self.case == "reserved-code" else "domain",
                            "private",
                        )
                    return RuleResult(True)

            limits = OutputLimits(
                max_issues=max_issues,
                max_invocations=1 if case in {"budget", "base-budget"} else 10,
            )
            sync = ()
            value, schema, path = "yes", OutputSchema("string"), ()
            if case in {"repair", "base-budget"}:
                sync, value = (RuleBinding("trim", (), TrimmedString(), "fix"),), " yes "
            if case == "optional":
                value, schema, path = (
                    {},
                    OutputSchema("object", properties={"x": OutputSchema("string")}),
                    ("x",),
                )
            if case == "schema":
                value = 12
            wrapper = AsyncValidationPipeline(
                ValidationPipeline(schema, sync, limits),
                tuple(
                    AsyncRuleBinding(f"async_{i}", path, AsyncCheck(i, calls, case))
                    for i in range(3)
                ),
                AsyncValidationPolicy(max_concurrency=2),
            )
            report = asyncio.run(wrapper.validate_json_bytes(canonical(value).encode()))
            transcripts.append(
                {
                    "case": f"async-{case}-{max_issues}",
                    "calls": calls,
                    "report": report.to_dict(include_output=True),
                }
            )
    return transcripts


def test_complete_signed_sync_generation_and_standalone_async_transcripts():
    expected = (Path(__file__).parent / "data" / "async-generation-e8733364.json").read_text(
        encoding="utf-8"
    )
    assert canonical(capture_signed_behavior()) == expected.strip()
