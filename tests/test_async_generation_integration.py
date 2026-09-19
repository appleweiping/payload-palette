from __future__ import annotations

import asyncio
from dataclasses import asdict, replace

import pytest
from tests._async_generation_helpers import Check, Provider, runner

from payload_palette import (
    AsyncGenerationRunner,
    AsyncRuleBinding,
    AsyncValidationPipeline,
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


def test_generation_accepts_exact_async_pipeline_and_returns_complete_output():
    requests = []

    class Provider:
        async def generate(self, request):
            requests.append(request)
            await asyncio.sleep(0)
            return GeneratedResponse('"accepted"', TokenUsage(2, 1))

    runner = AsyncGenerationRunner(
        AsyncValidationPipeline(ValidationPipeline(OutputSchema("string"))), Provider()
    )
    report = asyncio.run(runner.run("offline"))
    assert report.valid and report.output == "accepted"
    assert len(requests) == 1


def mixed_runner(maximum=10, *, local=100, max_issues=1):
    events = []

    class Trim:
        def check(self, value, context):
            events.append(["trim", context.phase, value])
            return TrimmedString().check(value, context)

    def catalog(value, context):
        events.append(["catalog", context.phase, value])
        return (
            RuleResult(True)
            if value == "yes"
            else RuleResult(False, "not_listed", "secret catalog")
        )

    def shape(value, context):
        events.append(["shape", context.phase, value])
        return RuleResult(True)

    instance, provider = runner(
        Check(catalog),
        Check(shape),
        responses=('" no "', '" yes "'),
        sync=(RuleBinding("trim", (), Trim(), "fix"),),
        limits=OutputLimits(max_invocations=local, max_issues=max_issues),
        policy=GenerationPolicy(max_validator_invocations=maximum, max_feedback=1),
    )
    return instance, provider, events


def test_repairs_once_then_awaited_catalog_reask_has_handwritten_complete_oracle():
    instance, provider, events = mixed_runner()
    report = asyncio.run(instance.run("offline"))
    assert report.to_dict(include_output=True) == {
        "valid": True,
        "termination": "accepted",
        "reported_tokens": 6,
        "response_bytes": 13,
        "validator_invocations": 10,
        "usage_complete": True,
        "output": "yes",
        "attempts": [
            {
                "number": 1,
                "status": "invalid_output",
                "usage": {"input_tokens": 2, "output_tokens": 1},
                "response_bytes": 6,
                "validator_invocations": 5,
                "feedback": [{"code": "not_listed", "path": "$"}],
            },
            {
                "number": 2,
                "status": "accepted",
                "usage": {"input_tokens": 2, "output_tokens": 1},
                "response_bytes": 7,
                "validator_invocations": 5,
                "feedback": [],
            },
        ],
    }
    assert events == [
        ["trim", "initial", " no "],
        ["trim", "repair", "no"],
        ["trim", "final", "no"],
        ["catalog", "final", "no"],
        ["shape", "final", "no"],
        ["trim", "initial", " yes "],
        ["trim", "repair", "yes"],
        ["trim", "final", "yes"],
        ["catalog", "final", "yes"],
        ["shape", "final", "yes"],
    ]
    for i, request in enumerate(provider.requests):
        assert asdict(request) == {
            "instruction": "offline",
            "attempt": i + 1,
            "max_output_tokens": 512,
            "remaining_total_tokens": 4096 - i * 3,
            "max_response_bytes": 1_000_000,
            "feedback": () if i == 0 else ({"code": "not_listed", "path": "$"},),
            "_schema_json": '{"type": "string"}',
        }
        fresh = request.schema
        fresh["type"] = "tampered"
        assert request.schema == {"type": "string"}
    assert "secret" not in str(report.to_dict()) + str([asdict(x) for x in provider.requests])


@pytest.mark.parametrize("maximum", range(1, 12))
def test_one_lifetime_budget_spans_repairs_checks_and_reasks(maximum):
    instance, provider, events = mixed_runner(maximum)
    report = asyncio.run(instance.run("offline"))
    assert report.validator_invocations == min(maximum, 10) == len(events)
    assert report.termination == ("accepted" if maximum >= 10 else "validator_budget")
    assert len(provider.requests) == (1 if maximum <= 5 else 2)
    assert sum(a.validator_invocations for a in report.attempts) == report.validator_invocations


@pytest.mark.parametrize("local", range(1, 7))
def test_original_local_pipeline_budget_is_not_lifted_by_generation(local):
    instance, provider, events = mixed_runner(20, local=local)
    report = asyncio.run(instance.run("offline"))
    assert report.termination == ("accepted" if local >= 5 else "validator_budget")
    assert report.validator_invocations == (10 if local >= 5 else local) == len(events)
    assert len(provider.requests) == (2 if local >= 5 else 1)


@pytest.mark.parametrize(
    "code",
    [
        "validator_budget",
        "validation_deadline",
        "validator_contract",
        "validator_timeout",
        "output_contract",
    ],
)
@pytest.mark.parametrize("synchronous", [False, True])
def test_user_reserved_looking_code_is_rejection_not_infrastructure(code, synchronous):
    def decide(value, context):
        return RuleResult(True) if value == "yes" else RuleResult(False, code, "private")

    class Sync:
        check = staticmethod(decide)

    instance, provider = runner(
        *(() if synchronous else (Check(decide),)),
        responses=('"no"', '"yes"'),
        sync=(RuleBinding("sync", (), Sync()),) if synchronous else (),
        limits=OutputLimits(max_issues=1),
        policy=GenerationPolicy(max_feedback=1),
    )
    report = asyncio.run(instance.run("offline"))
    assert report.valid and report.validator_invocations == 2
    assert [attempt.status for attempt in report.attempts] == ["invalid_output", "accepted"]
    assert provider.requests[1].feedback[0].code == code


def test_scheduler_budget_cause_survives_full_issue_prefix():
    rejection = Check(lambda value, context: RuleResult(False, "domain", "private"))
    instance, provider = runner(
        rejection, Check(), limits=OutputLimits(max_invocations=1, max_issues=1)
    )
    report = asyncio.run(instance.run("offline"))
    assert report.termination == "validator_budget" and len(provider.requests) == 1
    assert report.validator_invocations == 1
    assert [x.code for x in report.attempts[0].feedback] == ["domain"]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", ["lookup", "noncallable"])
def test_runtime_getter_is_not_replayed_by_attempt_wrapper_clone(asynchronous, failure):
    class Lookup:
        broken = False
        lookups = 0

        @property
        def check(self):
            self.lookups += 1
            if self.broken:
                if failure == "lookup":
                    raise RuntimeError("private")
                return 1
            if asynchronous:

                async def valid(value, context):
                    return RuleResult(True)
            else:

                def valid(value, context):
                    return RuleResult(True)

            return valid

    checker = Lookup()
    instance, provider = runner(
        *((checker,) if asynchronous else ()),
        sync=() if asynchronous else (RuleBinding("sync", (), checker),),
    )
    admitted = checker.lookups
    checker.broken = True
    report = asyncio.run(instance.run("offline"))
    assert checker.lookups == admitted + 1
    assert report.termination == "validator_error" and len(provider.requests) == 1
    assert report.validator_invocations == (0 if asynchronous else 1)


def test_optional_paths_skip_without_reservation_and_rule_free_reasks_use_zero_calls():
    provider = Provider("{}")
    wrapper = AsyncValidationPipeline(
        ValidationPipeline(OutputSchema("object", properties={"x": OutputSchema("string")})),
        (AsyncRuleBinding("optional", ("x",), Check()),),
    )
    report = asyncio.run(AsyncGenerationRunner(wrapper, provider).run("offline"))
    assert report.valid and report.validator_invocations == 0
    instance, provider = runner(
        responses=("1", '"yes"'), policy=GenerationPolicy(max_validator_invocations=1)
    )
    report = asyncio.run(instance.run("offline"))
    assert report.valid and report.validator_invocations == 0 and len(provider.requests) == 2


def test_zero_remaining_with_declared_optional_rule_stops_before_second_provider():
    wrapper = AsyncValidationPipeline(
        ValidationPipeline(OutputSchema("object", properties={"x": OutputSchema("string")})),
        (AsyncRuleBinding("optional", ("x",), Check(lambda v, c: RuleResult(False, "domain"))),),
    )
    provider = Provider('{"x":"no"}', "{}")
    report = asyncio.run(
        AsyncGenerationRunner(wrapper, provider, GenerationPolicy(max_validator_invocations=1)).run(
            "offline"
        )
    )
    assert report.termination == "validator_budget" and len(provider.requests) == 1


@pytest.mark.parametrize(
    "bad",
    [
        object(),
        ValidationPipeline(OutputSchema("string")),
        AsyncValidationPipeline(ValidationPipeline(OutputSchema("string"))),
    ],
)
def test_public_pipeline_union_remains_exact(bad):
    if type(bad) is object:
        with pytest.raises(ValueError):
            AsyncGenerationRunner(bad, Provider('"yes"'))
    else:
        subclass = type("Subclass", (type(bad),), {})
        child = subclass(bad.schema) if type(bad) is ValidationPipeline else subclass(bad.pipeline)
        with pytest.raises(ValueError):
            AsyncGenerationRunner(child, Provider('"yes"'))


def test_wrapped_pipeline_and_limits_not_mutated_by_lifetime_admission():
    instance, _, _ = mixed_runner(3)
    before = instance.pipeline.pipeline
    report = asyncio.run(instance.run("offline"))
    assert report.termination == "validator_budget"
    assert instance.pipeline.pipeline is before and before.limits.max_invocations == 100
    assert (
        replace(instance, policy=GenerationPolicy(max_validator_invocations=20)).pipeline
        is instance.pipeline
    )
