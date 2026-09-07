from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import pytest
from examples.generate_validated_output import run_example

from payload_palette import (
    AsyncGenerationRunner,
    GeneratedResponse,
    GenerationFeedback,
    GenerationPolicy,
    GenerationRequest,
    JSONValue,
    OutputLimits,
    OutputSchema,
    RuleBinding,
    RuleContext,
    RuleResult,
    StringChoices,
    TokenUsage,
    TrimmedString,
    ValidationPipeline,
)


class ScriptedProvider:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GeneratedResponse:
        self.requests.append(request)
        await asyncio.sleep(0)
        response = self.responses[len(self.requests) - 1]
        if isinstance(response, Exception):
            raise response
        return response


class Callback:
    def __init__(self, callback: Callable[[JSONValue, RuleContext], RuleResult]) -> None:
        self.callback = callback

    def check(self, value: JSONValue, context: RuleContext) -> RuleResult:
        return self.callback(value, context)


def response(text: str, input_tokens: int = 2, output_tokens: int = 1) -> GeneratedResponse:
    return GeneratedResponse(text, TokenUsage(input_tokens, output_tokens))


def test_offline_end_to_end_reask_example() -> None:
    report = asyncio.run(run_example())
    assert report["valid"] is True
    assert report["output"] == {"category": "image"}
    assert report["reported_tokens"] == 55
    assert [item["status"] for item in report["attempts"]] == ["invalid_output", "accepted"]


def test_generated_output_passes_semantic_repair_before_acceptance() -> None:
    provider = ScriptedProvider([response('" x "')])
    pipeline = ValidationPipeline(
        OutputSchema("string"), (RuleBinding("trim", (), TrimmedString(), "fix"),)
    )
    report = asyncio.run(AsyncGenerationRunner(pipeline, provider).run("instruction"))
    assert report.valid
    assert report.output == "x"
    assert report.validator_invocations == 3
    assert report.reported_tokens == 3
    assert report.usage_complete
    assert "output" not in report.to_dict()


def test_json_schema_and_semantic_failures_are_reasked_in_order() -> None:
    provider = ScriptedProvider(
        [response("not-json"), response('{"label":42}'), response('{"label":"yes"}')]
    )
    pipeline = ValidationPipeline(
        OutputSchema("object", properties={"label": OutputSchema("string")}, required=("label",))
    )
    report = asyncio.run(AsyncGenerationRunner(pipeline, provider).run("Please classify."))
    assert report.valid
    assert report.reported_tokens == 9
    assert [request.attempt for request in provider.requests] == [1, 2, 3]
    assert provider.requests[0].feedback == ()
    assert provider.requests[1].feedback == (GenerationFeedback("invalid_json"),)
    assert provider.requests[2].feedback == (GenerationFeedback("schema_type", '$["label"]'),)
    assert report.response_bytes == sum(len(item.text.encode()) for item in provider.responses)
    original_schema = provider.requests[0].schema
    original_schema.clear()
    assert provider.requests[0].schema["type"] == "object"
    returned = report.output
    assert isinstance(returned, dict)
    returned.clear()
    assert report.output == {"label": "yes"}


def test_history_and_reask_feedback_do_not_retain_raw_secrets() -> None:
    secret_key = "private_api_key"
    provider = ScriptedProvider(
        [response(json.dumps({secret_key: "very-secret-value"})), response('"no"')]
    )
    pipeline = ValidationPipeline(
        OutputSchema("object"),
    )
    report = asyncio.run(
        AsyncGenerationRunner(pipeline, provider, GenerationPolicy(max_attempts=2)).run(
            "private instruction"
        )
    )
    serialized = json.dumps(report.to_dict()) + repr(report) + repr(provider.requests[1])
    assert secret_key not in serialized
    assert "very-secret-value" not in serialized
    assert "private instruction" not in serialized
    assert provider.requests[1].instruction == "private instruction"
    assert provider.requests[1].feedback == (GenerationFeedback("schema_extra"),)
    assert "very-secret-value" not in repr(provider.responses[0])
    assert report.output is None


def test_custom_validator_messages_are_omitted_from_execution_history() -> None:
    def secret_message(value: JSONValue, context: RuleContext) -> RuleResult:
        return RuleResult(False, "bad_label", f"this contains {value}")

    provider = ScriptedProvider([response('"private-value"')])
    pipeline = ValidationPipeline(
        OutputSchema("string"), (RuleBinding("secret", (), Callback(secret_message)),)
    )
    report = asyncio.run(
        AsyncGenerationRunner(pipeline, provider, GenerationPolicy(max_attempts=1)).run(
            "private instruction"
        )
    )
    assert report.termination == "attempts_exhausted"
    assert report.attempts[0].feedback == (GenerationFeedback("bad_label"),)
    assert "private" not in str(report.to_dict())


def test_declared_nested_paths_and_feedback_caps() -> None:
    schema = OutputSchema(
        "object",
        properties={
            "rows": OutputSchema(
                "array", items=OutputSchema("object", properties={"label": OutputSchema("string")})
            )
        },
    )
    provider = ScriptedProvider(
        [response('{"rows":[{"label":1,"unknown":1},{"label":2,"unknown":2}]}')]
    )
    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(schema), provider, GenerationPolicy(max_attempts=1, max_feedback=2)
        ).run("classify")
    )
    assert report.attempts[0].feedback == (
        GenerationFeedback("schema_type", '$["rows"][0]["label"]'),
        GenerationFeedback("schema_extra"),
    )
    provider = ScriptedProvider([response('{"a":1,"b":2}')])
    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("object")), provider, GenerationPolicy(max_attempts=1)
        ).run("classify")
    )
    assert report.attempts[0].feedback == (GenerationFeedback("schema_extra"),)


@pytest.mark.parametrize(
    "failure", [RuntimeError("credential=secret"), TimeoutError("provider timed out")]
)
def test_provider_failures_terminate_without_leaking_errors_or_retrying(failure: Exception) -> None:
    provider = ScriptedProvider([failure])
    report = asyncio.run(
        AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), provider).run("input")
    )
    assert report.termination == "provider_error"
    assert not report.usage_complete
    assert len(provider.requests) == 1
    assert "credential" not in str(report.to_dict())


@pytest.mark.parametrize("wrong", [None, {}, True, object()])
def test_malformed_provider_response_is_rejected(wrong: Any) -> None:
    provider = ScriptedProvider([wrong])
    report = asyncio.run(
        AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), provider).run("input")
    )
    assert report.termination == "provider_contract"
    assert not report.usage_complete


def test_coroutine_returned_as_response_is_closed_and_rejected() -> None:
    async def inner() -> GeneratedResponse:
        return response("null")

    provider = ScriptedProvider([inner()])
    report = asyncio.run(
        AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), provider).run("input")
    )
    assert report.termination == "provider_contract"


def test_suspended_malformed_coroutine_cleanup_errors_are_sanitized() -> None:
    async def malformed() -> None:
        try:
            await asyncio.sleep(0)
        finally:
            raise RuntimeError("private-response-cleanup-secret")

    class Provider:
        async def generate(self, request: GenerationRequest) -> Any:
            coroutine = malformed()
            coroutine.send(None)
            return coroutine

    report = asyncio.run(
        AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), Provider()).run("input")
    )
    assert report.termination == "provider_contract"
    assert not report.usage_complete
    assert "private-response-cleanup-secret" not in str(report.to_dict())


@pytest.mark.parametrize("cancel", [False, True])
def test_malformed_coroutine_cleanup_preserves_base_exception_and_cancellation(
    cancel: bool,
) -> None:
    class Abort(BaseException):
        pass

    async def malformed() -> None:
        try:
            await asyncio.sleep(0)
        finally:
            if cancel:
                task = asyncio.current_task()
                assert task is not None
                task.cancel()
                raise RuntimeError("cleanup failure")
            raise Abort("abort")

    class Provider:
        async def generate(self, request: GenerationRequest) -> Any:
            coroutine = malformed()
            coroutine.send(None)
            return coroutine

    runner = AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), Provider())
    with pytest.raises(asyncio.CancelledError if cancel else Abort):
        asyncio.run(runner.run("input"))


def test_forged_response_usage_is_revalidated() -> None:
    forged = response("null")
    object.__setattr__(forged, "usage", None)
    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("null")), ScriptedProvider([forged])
        ).run("input")
    )
    assert report.termination == "provider_contract"


def test_reported_token_budget_and_output_hints() -> None:
    provider = ScriptedProvider([response("bad", 4, 2), response("null", 2, 2)])
    policy = GenerationPolicy(max_total_tokens=10, max_output_tokens=8)
    report = asyncio.run(
        AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), provider, policy).run(
            "input"
        )
    )
    assert report.valid
    assert report.reported_tokens == 10
    assert [request.max_output_tokens for request in provider.requests] == [8, 4]
    assert [request.remaining_total_tokens for request in provider.requests] == [10, 4]
    provider = ScriptedProvider([response("null", 9, 2)])
    report = asyncio.run(
        AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), provider, policy).run(
            "input"
        )
    )
    assert report.termination == "token_budget"
    assert report.reported_tokens == 11
    assert report.usage_complete
    assert report.output is None


def test_provider_exceeding_requested_output_token_limit_is_rejected() -> None:
    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("null")),
            ScriptedProvider([response("null", 0, 5)]),
            GenerationPolicy(max_output_tokens=4),
        ).run("input")
    )
    assert report.termination == "token_budget"


def test_budget_exhaustion_prevents_another_provider_invocation() -> None:
    provider = ScriptedProvider([response("bad", 1, 2)])
    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("null")), provider, GenerationPolicy(max_total_tokens=3)
        ).run("input")
    )
    assert report.termination == "token_budget"
    assert len(provider.requests) == 1
    provider = ScriptedProvider([response("bad")])
    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("null")),
            provider,
            GenerationPolicy(max_total_response_bytes=3),
        ).run("input")
    )
    assert report.termination == "response_budget"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("text", ['"hello"', '"你"', '"éé"', '"😀"'])
def test_per_response_limit_checks_utf8_not_only_characters(text: str) -> None:
    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("string")),
            ScriptedProvider([response(text)]),
            GenerationPolicy(max_response_bytes=4),
        ).run("input")
    )
    assert report.termination == "response_budget"


def test_cumulative_response_budget_is_shared_across_attempts() -> None:
    provider = ScriptedProvider([response("bad"), response("null")])
    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("null")),
            provider,
            GenerationPolicy(max_total_response_bytes=6),
        ).run("input")
    )
    assert report.termination == "response_budget"
    assert report.response_bytes == 7
    assert [request.max_response_bytes for request in provider.requests] == [6, 3]


def test_concurrent_runs_keep_budget_feedback_and_output_isolated() -> None:
    async def scenario() -> None:
        class Provider:
            async def generate(self, request: GenerationRequest) -> GeneratedResponse:
                await asyncio.sleep(0)
                if request.attempt == 1:
                    assert request.feedback == ()
                    return response("invalid")
                assert request.attempt == 2
                assert request.remaining_total_tokens == 3
                return response(json.dumps(request.instruction))

        runner = AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("string")),
            Provider(),
            GenerationPolicy(max_total_tokens=6),
        )
        left, right = await asyncio.gather(runner.run("left"), runner.run("right"))
        assert left.output == "left"
        assert right.output == "right"
        assert left.reported_tokens == right.reported_tokens == 6
        assert len(left.attempts) == len(right.attempts) == 2

    asyncio.run(scenario())


def test_input_snapshot_limit_generates_safe_reask_feedback() -> None:
    provider = ScriptedProvider([response('"large"'), response('"x"')])
    pipeline = ValidationPipeline(OutputSchema("string"), limits=OutputLimits(max_characters=1))
    report = asyncio.run(AsyncGenerationRunner(pipeline, provider).run("input"))
    assert report.valid
    assert provider.requests[1].feedback == (GenerationFeedback("output_contract"),)


def test_validator_invocation_budget_is_shared_across_reasks() -> None:
    pipeline = ValidationPipeline(
        OutputSchema("string"), (RuleBinding("choice", (), StringChoices(("yes",))),)
    )
    provider = ScriptedProvider([response('"no"')])
    report = asyncio.run(
        AsyncGenerationRunner(
            pipeline, provider, GenerationPolicy(max_validator_invocations=1)
        ).run("input")
    )
    assert report.termination == "validator_budget"
    assert report.validator_invocations == 1
    assert len(provider.requests) == 1


def test_validator_repair_budget_and_contract_errors_do_not_reask_provider() -> None:
    pipeline = ValidationPipeline(
        OutputSchema("string"), (RuleBinding("trim", (), TrimmedString(), "fix"),)
    )
    report = asyncio.run(
        AsyncGenerationRunner(
            pipeline,
            ScriptedProvider([response('" x "')]),
            GenerationPolicy(max_validator_invocations=1),
        ).run("input")
    )
    assert report.termination == "validator_budget"

    def broken(value: JSONValue, context: RuleContext) -> RuleResult:
        raise ValueError("secret")

    pipeline = ValidationPipeline(
        OutputSchema("null"), (RuleBinding("broken", (), Callback(broken)),)
    )
    report = asyncio.run(
        AsyncGenerationRunner(pipeline, ScriptedProvider([response("null")])).run("input")
    )
    assert report.termination == "validator_error"


def test_user_failure_code_does_not_impersonate_internal_budget_errors() -> None:
    def validator(value: JSONValue, context: RuleContext) -> RuleResult:
        return (
            RuleResult(True)
            if value == 1
            else RuleResult(False, "validator_budget", "domain failure")
        )

    pipeline = ValidationPipeline(
        OutputSchema("integer"), (RuleBinding("domain", (), Callback(validator)),)
    )
    report = asyncio.run(
        AsyncGenerationRunner(pipeline, ScriptedProvider([response("0"), response("1")])).run(
            "input"
        )
    )
    assert report.valid


@pytest.mark.parametrize("total", [False, True])
def test_timeout_cancels_and_awaits_provider_cleanup(total: bool) -> None:
    async def scenario() -> None:
        cleaned = asyncio.Event()

        class WaitingProvider:
            async def generate(self, request: GenerationRequest) -> GeneratedResponse:
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                raise AssertionError("unreachable")

        policy = GenerationPolicy(
            attempt_timeout_seconds=1 if total else 0.01, total_timeout_seconds=0.01 if total else 1
        )
        report = await AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("null")), WaitingProvider(), policy
        ).run("input")
        assert report.termination == ("deadline" if total else "timeout")
        assert cleaned.is_set()
        assert not report.usage_complete

    asyncio.run(scenario())


@pytest.mark.parametrize("total", [False, True])
def test_internal_timeout_remains_timeout_when_provider_cleanup_raises(total: bool) -> None:
    async def scenario() -> None:
        cleaned = asyncio.Event()

        class Provider:
            async def generate(self, request: GenerationRequest) -> GeneratedResponse:
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
                    raise RuntimeError("private cleanup failure")

        policy = GenerationPolicy(
            attempt_timeout_seconds=1 if total else 0.01, total_timeout_seconds=0.01 if total else 1
        )
        report = await AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("null")), Provider(), policy
        ).run("input")
        assert report.termination == ("deadline" if total else "timeout")
        assert report.attempts[0].status == "timeout"
        assert cleaned.is_set()
        assert not report.usage_complete
        assert "private cleanup failure" not in str(report.to_dict())

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["propagate", "return", "error", "timeout"])
def test_external_cancellation_propagates_even_if_adapter_suppresses_it(mode: str) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        class Provider:
            async def generate(self, request: GenerationRequest) -> GeneratedResponse:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if mode == "propagate":
                        raise
                    if mode == "error":
                        raise RuntimeError("cleanup failure") from None
                    if mode == "timeout":
                        raise TimeoutError("cleanup timeout") from None
                finally:
                    cleaned.set()
                return response("null")

        runner = AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), Provider())
        task = asyncio.create_task(runner.run("input"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()

    asyncio.run(scenario())


def test_adapter_cannot_turn_a_suppressed_deadline_into_success() -> None:
    class Provider:
        async def generate(self, request: GenerationRequest) -> GeneratedResponse:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return response("null")
            raise AssertionError("unreachable")

    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("null")),
            Provider(),
            GenerationPolicy(attempt_timeout_seconds=0.01),
        ).run("input")
    )
    assert report.termination == "timeout"


def test_event_loop_blocking_adapter_cannot_accept_late_output() -> None:
    class Provider:
        async def generate(self, request: GenerationRequest) -> GeneratedResponse:
            time.sleep(0.02)
            return response("null")

    report = asyncio.run(
        AsyncGenerationRunner(
            ValidationPipeline(OutputSchema("null")),
            Provider(),
            GenerationPolicy(attempt_timeout_seconds=0.001),
        ).run("input")
    )
    assert report.termination == "timeout"


def test_synchronous_validation_checks_deadline_before_acceptance() -> None:
    def slow(value: JSONValue, context: RuleContext) -> RuleResult:
        time.sleep(0.02)
        return RuleResult(True)

    pipeline = ValidationPipeline(OutputSchema("null"), (RuleBinding("slow", (), Callback(slow)),))
    report = asyncio.run(
        AsyncGenerationRunner(
            pipeline,
            ScriptedProvider([response("null")]),
            GenerationPolicy(total_timeout_seconds=0.005),
        ).run("input")
    )
    assert report.termination == "deadline"
    assert report.reported_tokens == 3
    assert report.usage_complete


@pytest.mark.parametrize("value", [None, 1, "\ud800", "long"])
def test_instruction_contract_is_checked_before_provider(value: Any) -> None:
    provider = ScriptedProvider([])
    runner = AsyncGenerationRunner(
        ValidationPipeline(OutputSchema("null")),
        provider,
        GenerationPolicy(max_instruction_characters=3),
    )
    with pytest.raises(ValueError):
        asyncio.run(runner.run(value))
    assert not provider.requests


@pytest.mark.parametrize(
    "field",
    [
        "max_attempts",
        "max_output_tokens",
        "max_total_tokens",
        "max_response_bytes",
        "max_total_response_bytes",
        "max_validator_invocations",
        "max_feedback",
        "max_instruction_characters",
    ],
)
@pytest.mark.parametrize("value", [0, True, 1.0, 10**20])
def test_generation_policy_integer_limits(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        GenerationPolicy(**{field: value})


@pytest.mark.parametrize("field", ["attempt_timeout_seconds", "total_timeout_seconds"])
@pytest.mark.parametrize("value", [0, True, float("nan"), float("inf"), 86_401, 10**1000, "1"])
def test_generation_policy_deadlines(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        GenerationPolicy(**{field: value})


@pytest.mark.parametrize("value", [-1, True, 1.0, 1_000_000_001])
def test_usage_contract(value: Any) -> None:
    with pytest.raises(ValueError):
        TokenUsage(value, 0)
    with pytest.raises(ValueError):
        TokenUsage(0, value)


def test_response_and_feedback_contracts() -> None:
    for value in (None, 1, "\ud800"):
        with pytest.raises(ValueError):
            GeneratedResponse(value, TokenUsage(0, 0))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GeneratedResponse("null", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        GenerationFeedback("UPPER")
    with pytest.raises(ValueError):
        GenerationFeedback("code", "not-a-path")
    with pytest.raises(ValueError):
        GenerationFeedback("code", "$\ud800")


def test_runner_requires_a_real_async_provider() -> None:
    class SyncProvider:
        def generate(self, request: GenerationRequest) -> GeneratedResponse:
            return response("null")

    with pytest.raises(ValueError, match="async"):
        AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), SyncProvider())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AsyncGenerationRunner(None, ScriptedProvider([]))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AsyncGenerationRunner(ValidationPipeline(OutputSchema("null")), ScriptedProvider([]), None)  # type: ignore[arg-type]
