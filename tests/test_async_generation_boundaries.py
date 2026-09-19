from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress

import pytest
from tests._async_generation_helpers import AwaitCheck, Check, runner

from payload_palette import (
    AsyncGenerationRunner,
    AsyncRuleBinding,
    AsyncValidationPipeline,
    AsyncValidationPolicy,
    GeneratedResponse,
    GenerationPolicy,
    OutputContractError,
    OutputLimits,
    OutputSchema,
    PayloadValidationError,
    RuleBinding,
    RuleResult,
    TokenUsage,
    TrimmedString,
    ValidationIssue,
    ValidationPipeline,
    async_validation,
    generation,
    output_validation,
)


@pytest.mark.parametrize("concurrency", [1, 2, 4])
def test_completion_order_cannot_reorder_feedback_and_all_children_finish_before_reask(concurrency):
    async def scenario():
        entered, finished = [], []
        gates = [asyncio.Event() for _ in range(4)]
        changed = asyncio.Event()

        class Controlled:
            def __init__(self, index):
                self.index = index

            async def check(self, value, context):
                if value == "yes":
                    return RuleResult(True)
                entered.append(self.index)
                changed.set()
                try:
                    await gates[self.index].wait()
                    return RuleResult(False, f"rule_{self.index}", "private")
                finally:
                    await asyncio.sleep(0)
                    finished.append(self.index)
                    changed.set()

        class Provider:
            calls = 0

            async def generate(self, request):
                self.calls += 1
                if self.calls == 2:
                    assert sorted(finished) == list(range(4))
                    assert [item.code for item in request.feedback] == [
                        f"rule_{i}" for i in range(4)
                    ]
                return GeneratedResponse('"no"' if self.calls == 1 else '"yes"', TokenUsage(2, 1))

        instance, _ = runner(
            *(Controlled(i) for i in range(4)),
            async_policy=AsyncValidationPolicy(max_concurrency=concurrency),
        )
        provider = Provider()
        task = asyncio.create_task(
            AsyncGenerationRunner(instance.pipeline, provider).run("offline")
        )
        while len(finished) < 4:
            await changed.wait()
            changed.clear()
            waiting = [i for i in entered if not gates[i].is_set()]
            if waiting:
                gates[max(waiting)].set()
        report = await task
        assert report.valid and report.validator_invocations == 8 and provider.calls == 2
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["future", "task", "started-coroutine"])
def test_changed_check_factory_return_preserves_borrowed_resource_ownership(kind):
    async def scenario():
        checker = Check()
        instance, _ = runner(checker)
        if kind == "future":
            borrowed = asyncio.get_running_loop().create_future()
        elif kind == "task":
            borrowed = asyncio.create_task(asyncio.Event().wait())
        else:

            async def suspended():
                await asyncio.sleep(0)

            borrowed = suspended()
            borrowed.send(None)
        checker.check = lambda value, context: borrowed
        try:
            report = await instance.run("offline")
            assert report.termination == "validator_error" and report.validator_invocations == 1
            if kind == "started-coroutine":
                assert inspect.getcoroutinestate(borrowed) == inspect.CORO_SUSPENDED
            else:
                assert not borrowed.done()
                if kind == "task":
                    assert borrowed.cancelling() == 0
        finally:
            if kind == "started-coroutine":
                borrowed.close()
            else:
                borrowed.cancel()
                with suppress(asyncio.CancelledError):
                    await borrowed

    asyncio.run(scenario())


@pytest.mark.parametrize("as_fix", [False, True])
def test_visible_malformed_native_coroutine_is_released_after_validation(as_fix):
    async def scenario():
        body = []

        async def never_enter():
            body.append(True)

        malformed = never_enter()
        instance, _ = runner(
            Check(lambda v, c: RuleResult(False, "domain", fix=malformed) if as_fix else malformed)
        )
        report = await instance.run("offline")
        assert report.termination == "validator_error" and report.validator_invocations == 1
        assert inspect.getcoroutinestate(malformed) == inspect.CORO_CLOSED and not body

    asyncio.run(scenario())


def test_private_attempt_limits_reach_existing_worker_snapshot(monkeypatch):
    observed = []
    original = async_validation.snapshot_json

    def snapshot(value, limits):
        observed.append(limits.max_invocations)
        return original(value, limits)

    monkeypatch.setattr(async_validation, "snapshot_json", snapshot)
    instance, _ = runner(
        Check(),
        limits=OutputLimits(max_invocations=100),
        policy=GenerationPolicy(max_validator_invocations=3),
    )
    assert asyncio.run(instance.run("offline")).valid
    assert observed == [3, 3]  # One ingress boundary copy, one worker snapshot.
    assert instance.pipeline.pipeline.limits.max_invocations == 100


def test_reserved_worker_snapshot_deadline_charges_zero_entries(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        now = [loop.time()]
        monkeypatch.setattr(loop, "time", lambda: now[0])
        original = async_validation.snapshot_json
        invoked, copied = [], []

        def snapshot(value, limits):
            result = original(value, limits)
            copied.append(value)
            if len(copied) == 2:
                now[0] += 11
            return result

        monkeypatch.setattr(async_validation, "snapshot_json", snapshot)
        instance, _ = runner(
            Check(lambda v, c: invoked.append(True)),
            policy=GenerationPolicy(total_timeout_seconds=10),
        )
        report = await instance.run("offline")
        assert report.termination == "deadline" and report.validator_invocations == 0
        assert len(copied) == 2 and not invoked and len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["base", "async"])
def test_private_envelope_distinguishes_trusted_budget_from_rejected_user_code(stage):
    async def scenario():
        if stage == "base":
            wrapper = AsyncValidationPipeline(
                ValidationPipeline(
                    OutputSchema("string"),
                    (RuleBinding("trim", (), TrimmedString(), "fix"),),
                    OutputLimits(max_invocations=1),
                )
            )
            execution = await wrapper._validate_execution(" yes ")
        else:
            instance, _ = runner(Check(), Check(), limits=OutputLimits(max_invocations=1))
            execution = await instance.pipeline._validate_execution("yes")
        assert execution.terminal_cause == "validator_budget" and execution.deadline_origin is None
        assert not execution.report.valid and execution.report.invocations == 1
        rejected, _ = runner(Check(lambda v, c: RuleResult(False, "validator_budget")))
        normal = await rejected.pipeline._validate_execution("no")
        assert normal.terminal_cause is None and normal.deadline_origin is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload,maximum,accepted",
    [('"é"', 4, True), ('"é"', 3, False), ('"😀é"', 8, True), ('"😀é"', 7, False)],
)
def test_utf8_response_bytes_are_counted_once_before_semantics(
    payload, maximum, accepted, monkeypatch
):
    # Python's independent encoder is the oracle, not the production counter.
    byte_count = len(payload.encode("utf-8"))
    measured, decoded = [], []
    utf8, decode = generation._utf8_size, async_validation.decode_json_bytes

    def measure(text):
        measured.append(text)
        return utf8(text)

    def decode_once(value, **kwargs):
        decoded.append(value)
        return decode(value, **kwargs)

    monkeypatch.setattr(generation, "_utf8_size", measure)
    monkeypatch.setattr(async_validation, "decode_json_bytes", decode_once)
    instance, _ = runner(
        Check(), responses=(payload,), policy=GenerationPolicy(max_response_bytes=maximum)
    )
    report = asyncio.run(instance.run("offline"))
    assert report.valid is accepted and measured == [payload]
    assert decoded == ([payload.encode()] if accepted else [])
    assert report.response_bytes == byte_count
    assert report.validator_invocations == int(accepted)


@pytest.mark.parametrize("code", ["validator_contract", "validation_deadline"])
def test_semantic_feedback_never_discloses_undeclared_key_or_validator_message(code):
    schema = OutputSchema("object", properties={"x": OutputSchema("string")})
    instance, provider = runner(responses=('{"private-key":"secret"}', "{}"), schema=schema)
    report = asyncio.run(instance.run("offline"))
    assert report.valid and len(provider.requests) == 2
    assert "private-key" not in str(report.to_dict()) + str(provider.requests[1].feedback)
    wrapper = AsyncValidationPipeline(
        ValidationPipeline(schema),
        (
            AsyncRuleBinding(
                "field", ("x",), Check(lambda v, c: RuleResult(False, code, "private-value"))
            ),
        ),
    )
    from tests._async_generation_helpers import Provider

    provider = Provider('{"x":"private-value"}', "{}")
    report = asyncio.run(AsyncGenerationRunner(wrapper, provider).run("offline"))
    assert report.valid and report.validator_invocations == 1
    assert [(f.code, f.path) for f in provider.requests[1].feedback] == [(code, '$["x"]')]
    assert "private-value" not in str(report.to_dict()) + str(provider.requests[1].feedback)


def test_distinct_runs_share_no_mutable_counts_or_feedback():
    async def scenario():
        checker = Check()
        instances = [runner(checker, responses=(f'"value-{i}"',)) for i in range(8)]
        reports = await asyncio.gather(*(instance.run("offline") for instance, _ in instances))
        assert [r.output for r in reports] == [f"value-{i}" for i in range(8)]
        assert all(
            r.valid and r.validator_invocations == 1 and r.reported_tokens == 3 for r in reports
        )
        assert all(
            len(provider.requests) == 1 and not provider.requests[0].feedback
            for _, provider in instances
        )

    asyncio.run(scenario())


def test_unmodified_wall_clock_deadline_waits_for_every_entered_finally():
    async def scenario():
        entered, closed = [], []

        async def check(value, context):
            entered.append(context.rule_id)
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.append(context.rule_id)

        instance, _ = runner(
            *(AwaitCheck(check) for _ in range(3)),
            async_policy=AsyncValidationPolicy(total_timeout_seconds=0.03),
            policy=GenerationPolicy(total_timeout_seconds=30),
        )
        report = await instance.run("offline")
        assert report.termination == "validator_error" and not report.valid
        assert sorted(entered) == sorted(closed)
        assert report.validator_invocations == len(entered)
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("owner", ["validation", "generation"])
def test_late_valid_ingress_copy_never_enters_sync_or_async_callbacks(owner, monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        now, called = [loop.time()], []
        monkeypatch.setattr(loop, "time", lambda: now[0])
        original = async_validation.snapshot_json

        def snapshot(value, limits):
            result = original(value, limits)
            now[0] += 11
            return result

        class Sync:
            def check(self, value, context):
                called.append("sync")
                return RuleResult(True)

        monkeypatch.setattr(async_validation, "snapshot_json", snapshot)
        instance, provider = runner(
            Check(lambda v, c: called.append("async")),
            sync=(RuleBinding("sync", (), Sync()),),
            async_policy=AsyncValidationPolicy(
                total_timeout_seconds=10 if owner == "validation" else 100
            ),
            policy=GenerationPolicy(total_timeout_seconds=10 if owner == "generation" else 100),
        )
        report = await instance.run("offline")
        assert report.termination == ("deadline" if owner == "generation" else "validator_error")
        assert report.validator_invocations == 0 and not called and len(provider.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("error_class", [OutputContractError, PayloadValidationError])
def test_same_class_failure_after_sync_callback_is_not_structural_reask(error_class, monkeypatch):
    error = (
        error_class((ValidationIssue("private", "internal"),))
        if error_class is PayloadValidationError
        else error_class("internal")
    )
    called = []
    original = output_validation.json.dumps

    class Sync:
        def check(self, value, context):
            called.append(value)
            return RuleResult(True)

    def fail_after_callback(value, **kwargs):
        if called and value == "yes" and kwargs.get("separators") == (",", ":"):
            raise error
        return original(value, **kwargs)

    monkeypatch.setattr(output_validation.json, "dumps", fail_after_callback)
    instance, provider = runner(Check(), sync=(RuleBinding("sync", (), Sync()),))
    with pytest.raises(error_class) as caught:
        asyncio.run(instance.run("offline"))
    assert caught.value is error and called == ["yes"] and len(provider.requests) == 1


def test_schema_work_failure_after_ingress_is_not_misreported_as_empty_invocation_attempt():
    instance, provider = runner(
        Check(),
        responses=('["yes"]',),
        schema=OutputSchema("array", items=OutputSchema("string")),
        limits=OutputLimits(max_schema_steps=1),
    )
    with pytest.raises(OutputContractError):
        asyncio.run(instance.run("offline"))
    assert len(provider.requests) == 1


@pytest.mark.parametrize("error_first", [False, True])
@pytest.mark.parametrize("owner", ["validation", "generation"])
def test_scheduler_deadline_provenance_survives_full_issue_prefix(owner, error_first, monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        now, entered = [loop.time()], asyncio.Event()
        monkeypatch.setattr(loop, "time", lambda: now[0])

        def first(value, context):
            if error_first:
                raise ValueError("private")
            return RuleResult(False, "domain", "private")

        async def second(value, context):
            entered.set()
            await asyncio.Event().wait()

        instance, provider = runner(
            Check(first),
            AwaitCheck(second),
            limits=OutputLimits(max_issues=1),
            async_policy=AsyncValidationPolicy(
                total_timeout_seconds=10 if owner == "validation" else 100,
                per_validator_timeout_seconds=200,
            ),
            policy=GenerationPolicy(total_timeout_seconds=10 if owner == "generation" else 100),
        )
        task = asyncio.create_task(instance.run("offline"))
        await entered.wait()
        for _ in range(3):
            await asyncio.sleep(0)
        now[0] += 11
        report = await task
        assert report.termination == ("deadline" if owner == "generation" else "validator_error")
        assert report.validator_invocations == 2 and len(provider.requests) == 1
        if owner == "validation":
            assert [x.code for x in report.attempts[0].feedback] == [
                "validator_contract" if error_first else "domain"
            ]

    asyncio.run(scenario())


def test_generation_deadline_during_response_measurement_never_starts_validation(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        now, snapshots = [loop.time()], []
        monkeypatch.setattr(loop, "time", lambda: now[0])
        measure, snapshot = generation._utf8_size, async_validation.snapshot_json

        def delayed(text):
            size = measure(text)
            now[0] += 11
            return size

        def copy(value, limits):
            snapshots.append(value)
            return snapshot(value, limits)

        monkeypatch.setattr(generation, "_utf8_size", delayed)
        monkeypatch.setattr(async_validation, "snapshot_json", copy)
        instance, provider = runner(Check(), policy=GenerationPolicy(total_timeout_seconds=10))
        report = await instance.run("offline")
        assert report.termination == "deadline" and report.attempts[0].status == "timeout"
        assert report.validator_invocations == 0 and report.response_bytes == 5
        assert report.reported_tokens == 3 and report.usage_complete
        assert not snapshots and len(provider.requests) == 1

    asyncio.run(scenario())
