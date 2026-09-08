from __future__ import annotations

import asyncio
import inspect
import json
import random
from contextlib import suppress
from dataclasses import FrozenInstanceError

import pytest

from payload_palette import (
    AsyncRuleBinding,
    AsyncValidationPipeline,
    AsyncValidationPolicy,
    OutputContractError,
    OutputLimits,
    OutputSchema,
    PayloadValidationError,
    RuleBinding,
    RuleResult,
    StringChoices,
    TrimmedString,
    ValidationPipeline,
)


class Check:
    def __init__(self, function=lambda value, context: RuleResult(True)):
        self.function = function

    async def check(self, value, context):
        return self.function(value, context)


class AwaitCheck:
    def __init__(self, function):
        self.function = function

    async def check(self, value, context):
        return await self.function(value, context)


class SyncCheck:
    def __init__(self, function):
        self.function = function

    def check(self, value, context):
        return self.function(value, context)


def build(*rules, schema=None, limits=None, policy=None, sync=()):
    return AsyncValidationPipeline(
        ValidationPipeline(
            schema or OutputSchema("object", additional_properties=True),
            sync,
            limits or OutputLimits(),
        ),
        rules,
        policy or AsyncValidationPolicy(),
    )


def controlled_clock(monkeypatch):
    """Drive real asyncio timer callbacks after a known callback-entry boundary."""
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    monkeypatch.setattr(loop, "time", lambda: now[0])

    def advance():
        now[0] += 20

    return advance


def test_sync_repairs_finish_before_async_checks_and_result_is_isolated():
    seen = []

    def check(value, context):
        seen.append((value.copy(), context.phase))
        value["label"] = "mutated"
        return RuleResult(True)

    pipeline = build(
        AsyncRuleBinding("observe", (), Check(check)),
        schema=OutputSchema(
            "object", properties={"label": OutputSchema("string")}, required=("label",)
        ),
        sync=(RuleBinding("trim", ("label",), TrimmedString(), "fix"),),
    )
    value = {"label": " hello "}
    report = asyncio.run(pipeline.validate(value))
    assert report.valid and report.output == {"label": "hello"}
    assert value == {"label": " hello "} and seen == [({"label": "hello"}, "final")]
    assert report.invocations == 4
    output = report.output
    output["label"] = "changed"
    assert report.output == {"label": "hello"}
    assert "output" not in report.to_dict()


def test_sync_rejection_including_later_invalidating_fix_schedules_nothing():
    called = []
    schema = OutputSchema("string")
    pipeline = build(
        AsyncRuleBinding("async", (), Check(lambda v, c: called.append(True))),
        schema=schema,
        sync=(
            RuleBinding("upper", (), StringChoices(("YES",))),
            RuleBinding("lower", (), StringChoices(("yes",), fix_case=True), "fix"),
        ),
    )
    for value in ("YES", 3):
        report = asyncio.run(pipeline.validate(value))
        assert not report.valid and not called


@pytest.mark.parametrize("concurrency", [1, 2, 4, 32])
def test_real_overlap_bounded_tasks_and_reverse_completion_order(concurrency):
    async def run():
        active = maximum = 0
        entered = []
        gates = [asyncio.Event() for _ in range(9)]
        changed = asyncio.Event()

        def validator(index):
            async def check(value, context):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                entered.append(index)
                changed.set()
                try:
                    await gates[index].wait()
                    return (
                        RuleResult(False, "rejected", str(index)) if index % 2 else RuleResult(True)
                    )
                finally:
                    active -= 1

            return AwaitCheck(check)

        pipeline = build(
            *(AsyncRuleBinding(f"r{i}", (), validator(i)) for i in range(9)),
            policy=AsyncValidationPolicy(max_concurrency=concurrency),
        )
        task = asyncio.create_task(pipeline.validate({"secret": "never mutated"}))
        released = set()
        while not task.done():
            await changed.wait()
            changed.clear()
            for index in reversed(entered.copy()):
                if index not in released:
                    released.add(index)
                    gates[index].set()
            if len(released) == 9:
                break
        report = await task
        assert maximum == min(concurrency, 9) and active == 0
        assert entered == list(range(9))
        assert [out.rule_id for out in report.outcomes] == [f"r{i}" for i in range(9)]
        assert [issue.message for issue in report.issues] == ["1", "3", "5", "7"]
        assert report.invocations == 9 and not report.valid and report.output is None

    asyncio.run(run())


def test_independent_seeded_sync_async_reject_rule_differential():
    rng = random.Random(827)
    for _ in range(35):
        value = {str(i): rng.randrange(-5, 6) for i in range(4)}
        schema = OutputSchema("object", additional_properties=OutputSchema("integer"))
        functions = [
            lambda v, c, m=i + 2: (
                RuleResult(True) if v % m else RuleResult(False, "multiple", str(m))
            )
            for i in range(4)
        ]
        sync = ValidationPipeline(
            schema,
            tuple(RuleBinding(f"r{i}", (str(i),), SyncCheck(fn)) for i, fn in enumerate(functions)),
        )
        async_pipeline = build(
            *(AsyncRuleBinding(f"r{i}", (str(i),), Check(fn)) for i, fn in enumerate(functions)),
            schema=schema,
        )
        before = sync.validate(value)
        after = asyncio.run(async_pipeline.validate(value))
        assert (after.valid, after.output, after.issues, after.invocations) == (
            before.valid,
            before.output,
            before.issues,
            before.invocations,
        )
        assert [out.status for out in after.outcomes] == [out.status for out in before.outcomes]


@pytest.mark.parametrize("limit", [1, 2, 4])
def test_budget_reservations_run_in_order_and_actual_calls_share_sync_budget(limit):
    called = []
    pipeline = build(
        *(
            AsyncRuleBinding(
                f"r{i}", (), Check(lambda v, c: (called.append(c.rule_id), RuleResult(True))[1])
            )
            for i in range(4)
        ),
        limits=OutputLimits(max_invocations=limit),
        sync=(RuleBinding("sync", (), SyncCheck(lambda v, c: RuleResult(True))),),
    )
    report = asyncio.run(pipeline.validate({}))
    assert called == [f"r{i}" for i in range(limit - 1)]
    assert report.invocations == limit and not report.valid
    assert any(issue.code == "validator_budget" for issue in report.issues)


def test_issue_cap_still_rejects_and_outcome_count_is_bounded():
    pipeline = build(
        *(
            AsyncRuleBinding(f"r{i}", (), Check(lambda v, c: RuleResult(False, "no", "no")))
            for i in range(8)
        ),
        limits=OutputLimits(max_issues=1),
    )
    report = asyncio.run(pipeline.validate({}))
    assert not report.valid and len(report.issues) == 1 and len(report.outcomes) == 8


def test_empty_and_optional_paths_and_null_acceptance():
    schema = OutputSchema(
        "union", any_of=(OutputSchema("null"), OutputSchema("array", items=OutputSchema("integer")))
    )
    pipeline = build(AsyncRuleBinding("optional", (3,), Check()), schema=schema)
    for value in (None, [], [1, 2]):
        report = asyncio.run(pipeline.validate(value))
        assert report.valid and report.invocations == 0 and report.output == value
    assert asyncio.run(build().validate({})).valid


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_concurrency", 0),
        ("max_concurrency", 33),
        ("max_concurrency", True),
        ("total_timeout_seconds", 0),
        ("total_timeout_seconds", float("nan")),
        ("total_timeout_seconds", float("inf")),
        ("total_timeout_seconds", 10**1000),
        ("per_validator_timeout_seconds", -1),
        ("per_validator_timeout_seconds", "2"),
    ],
)
def test_policy_types_and_finite_bounded_values(field, value):
    with pytest.raises(ValueError):
        AsyncValidationPolicy(**{field: value})


@pytest.mark.parametrize(
    "rule_id,path",
    [
        ("Bad", ()),
        ("", ()),
        (1, ()),
        ("r", []),
        ("r", (True,)),
        ("r", (-1,)),
        ("r", (100_000,)),
        ("r", ("x" * 257,)),
        ("r", ("\ud800",)),
        ("r", (0,) * 33),
    ],
)
def test_binding_strict_identity_and_path(rule_id, path):
    with pytest.raises(ValueError):
        AsyncRuleBinding(rule_id, path, Check())


@pytest.mark.parametrize("validator", [None, object(), SyncCheck(lambda v, c: RuleResult(True))])
def test_declared_async_method_required(validator):
    with pytest.raises(ValueError):
        AsyncRuleBinding("r", (), validator)


def test_pipeline_configuration_and_immutability():
    rule = AsyncRuleBinding("r", (), Check())
    for factory in (
        lambda: AsyncValidationPipeline(object()),
        lambda: AsyncValidationPipeline(ValidationPipeline(OutputSchema("null")), policy=object()),
        lambda: build(rule, rule),
        lambda: build(rule, sync=(RuleBinding("r", (), SyncCheck(lambda v, c: RuleResult(True))),)),
        lambda: build(*(rule for _ in range(257))),
        lambda: build(object()),
        lambda: build(AsyncRuleBinding("r", ("typo",), Check()), schema=OutputSchema("object")),
        lambda: build(AsyncRuleBinding("r", (0,), Check()), schema=OutputSchema("string")),
    ):
        with pytest.raises(ValueError):
            factory()
    with pytest.raises(FrozenInstanceError):
        rule.path = ("changed",)


@pytest.mark.parametrize("result", [None, True, {}, RuleResult(False, "bad", "repair", 1)])
def test_malformed_results_and_unsupported_repairs(result):
    report = asyncio.run(build(AsyncRuleBinding("r", (), Check(lambda v, c: result))).validate({}))
    assert not report.valid and report.invocations == 1
    assert report.issues[0].code == "validator_contract"


def test_ordinary_exception_and_invalid_result_prose_are_sanitized():
    def bad(value, context):
        raise ValueError("do-not-log-secret")

    report = asyncio.run(build(AsyncRuleBinding("r", (), Check(bad))).validate({"secret": 1}))
    assert "do-not-log-secret" not in json.dumps(report.to_dict())
    assert report.outcomes[0].status == "error"


@pytest.mark.parametrize("nested_fix", [False, True])
def test_nested_coroutine_return_or_fix_is_closed_without_execution(nested_fix):
    held = []
    ran = []

    async def nested():
        ran.append(True)

    def result(value, context):
        coroutine = nested()
        held.append(coroutine)
        return RuleResult(False, "repair", "", coroutine) if nested_fix else coroutine

    report = asyncio.run(build(AsyncRuleBinding("r", (), Check(result))).validate({}))
    assert not report.valid and not ran
    with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
        held[0].send(None)


@pytest.mark.parametrize("borrowed_kind", ["future", "task", "started_coroutine"])
def test_borrowed_awaitables_are_neither_awaited_nor_cancelled(borrowed_kind):
    async def run():
        async def other():
            await asyncio.sleep(0)
            return 7

        if borrowed_kind == "future":
            borrowed = asyncio.get_running_loop().create_future()
        elif borrowed_kind == "task":
            borrowed = asyncio.create_task(other())
        else:
            borrowed = other()
            borrowed.send(None)
        check = Check()
        pipeline = build(AsyncRuleBinding("r", (), check))
        check.check = lambda v, c: borrowed
        report = await pipeline.validate({})
        assert not report.valid and report.issues[0].code == "validator_contract"
        if borrowed_kind == "started_coroutine":
            assert inspect.getcoroutinestate(borrowed) == inspect.CORO_SUSPENDED
            borrowed.close()
        elif borrowed_kind == "future":
            assert not borrowed.done()
            borrowed.set_result(7)
        else:
            assert await borrowed == 7

    asyncio.run(run())


@pytest.mark.parametrize("total", [False, True])
@pytest.mark.parametrize("cleanup_error", [False, True])
def test_real_deadline_cancellation_awaits_cleanup_and_classifies_owner(
    total, cleanup_error, monkeypatch
):
    closed = []

    async def run():
        advance = controlled_clock(monkeypatch)

        async def check(value, context):
            try:
                asyncio.get_running_loop().call_soon(advance)
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.append(True)
                if cleanup_error:
                    raise RuntimeError("private-cleanup")

        policy = AsyncValidationPolicy(
            per_validator_timeout_seconds=100 if total else 10,
            total_timeout_seconds=10 if total else 100,
        )
        return await build(AsyncRuleBinding("r", (), AwaitCheck(check)), policy=policy).validate({})

    report = asyncio.run(run())
    assert not report.valid and closed == [True]
    assert report.invocations == 1
    assert any(
        issue.code == ("validation_deadline" if total else "validator_timeout")
        for issue in report.issues
    )
    assert "private-cleanup" not in json.dumps(report.to_dict())


def test_late_synchronous_stage_is_rejected_before_async_callbacks(monkeypatch):
    called = []

    def slow(value, context):
        loop = asyncio.get_running_loop()
        clock = loop.time
        monkeypatch.setattr(loop, "time", lambda: clock() + 20)
        return RuleResult(True)

    report = asyncio.run(
        build(
            AsyncRuleBinding("r", (), Check(lambda v, c: called.append(True))),
            sync=(RuleBinding("slow", (), SyncCheck(slow)),),
            policy=AsyncValidationPolicy(total_timeout_seconds=10),
        ).validate({})
    )
    assert not report.valid and not called and report.invocations == 1
    assert report.issues[-1].code == "validation_deadline"


@pytest.mark.parametrize("translate", [False, True])
def test_swallowed_callback_cancellation_never_approves(translate):
    async def check(value, context):
        asyncio.current_task().cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            if translate:
                raise ValueError("translated") from None
            return RuleResult(True)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(build(AsyncRuleBinding("r", (), AwaitCheck(check))).validate({}))


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_child_control_exception_settles_sibling_before_parent_raises(control):
    async def run():
        started = asyncio.Event()
        closed = []

        async def fail(value, context):
            await started.wait()
            raise control("requested-stop")

        async def sibling(value, context):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.append(True)

        pipeline = build(
            AsyncRuleBinding("fail", (), AwaitCheck(fail)),
            AsyncRuleBinding("sibling", (), AwaitCheck(sibling)),
        )
        with pytest.raises(control, match="requested-stop"):
            await pipeline.validate({})
        assert closed == [True]

    asyncio.run(run())


def test_repeated_parent_cancel_does_not_interrupt_owned_sibling_cleanup():
    async def run():
        entered, cleaning, release = (asyncio.Event() for _ in range(3))
        finished = []

        async def check(value, context):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                finished.append(True)

        task = asyncio.create_task(build(AsyncRuleBinding("r", (), AwaitCheck(check))).validate({}))
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == [True] and task.cancelling() == 2
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(run())


def test_concurrent_runs_do_not_share_reservations_counters_or_values():
    async def check(value, context):
        await asyncio.sleep(0)
        value["seen"] = True
        return RuleResult(True)

    async def run():
        pipeline = build(AsyncRuleBinding("r", (), AwaitCheck(check)))
        reports = await asyncio.gather(*(pipeline.validate({"id": i}) for i in range(12)))
        assert [report.output for report in reports] == [{"id": i} for i in range(12)]
        assert all(report.valid and report.invocations == 1 for report in reports)

    asyncio.run(run())


@pytest.mark.parametrize("value", [object(), float("nan"), {"a": "\ud800"}])
def test_invalid_python_json_retains_existing_contract(value):
    with pytest.raises(OutputContractError):
        asyncio.run(build().validate(value))


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b"{", b"NaN", b'"\xff"'])
def test_strict_json_ingress_retains_existing_errors(raw):
    with pytest.raises(PayloadValidationError):
        asyncio.run(build().validate_json_bytes(raw))


def test_byte_api_and_no_awaitable_repair_coercion():
    report = asyncio.run(build().validate_json_bytes(memoryview(b'{"a":1}')))
    assert report.valid and report.output == {"a": 1}
    with pytest.raises(PayloadValidationError):
        asyncio.run(build().validate_json_bytes(b'{"a":1}', max_input_bytes=1))


def test_semantic_code_cannot_impersonate_scheduler_timeout():
    called = []
    first = Check(lambda v, c: RuleResult(False, "validation_deadline", "application code"))
    second = Check(lambda v, c: (called.append(True), RuleResult(True))[1])
    report = asyncio.run(
        build(
            AsyncRuleBinding("first", (), first),
            AsyncRuleBinding("second", (), second),
            policy=AsyncValidationPolicy(max_concurrency=1),
        ).validate({})
    )
    assert not report.valid and called == [True] and report.invocations == 2


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_genuine_cleanup_control_overrides_ordinary_scheduler_failure(monkeypatch, control):
    async def run():
        entered = asyncio.Event()

        async def check(value, context):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                raise control("cleanup-control")

        async def broken_wait(*args, **kwargs):
            await entered.wait()
            raise OSError("private-infrastructure-error")

        monkeypatch.setattr(asyncio, "wait", broken_wait)
        with pytest.raises(control, match="cleanup-control"):
            await build(AsyncRuleBinding("r", (), AwaitCheck(check))).validate({})
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(run())


def test_first_control_preserved_when_sibling_cleanup_also_interrupts():
    async def run():
        started = asyncio.Event()

        async def first(value, context):
            await started.wait()
            raise KeyboardInterrupt("first-control")

        async def second(value, context):
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                raise SystemExit("second-control")

        with pytest.raises(KeyboardInterrupt, match="first-control"):
            await build(
                AsyncRuleBinding("a", (), AwaitCheck(first)),
                AsyncRuleBinding("b", (), AwaitCheck(second)),
            ).validate({})

    asyncio.run(run())


def test_malformed_exact_result_still_closes_directly_visible_coroutine_fix():
    held = []

    async def misplaced():
        raise AssertionError("must not execute")

    def bad(value, context):
        coroutine = misplaced()
        held.append(coroutine)
        result = RuleResult(False, "no", "", coroutine)
        object.__setattr__(result, "valid", 1)
        return result

    report = asyncio.run(build(AsyncRuleBinding("r", (), Check(bad))).validate({}))
    assert not report.valid and report.issues[0].code == "validator_contract"
    with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
        held[0].send(None)


@pytest.mark.parametrize("ordinary_cleanup", [False, True])
def test_malformed_suspended_nested_coroutine_cleanup_is_sanitized(ordinary_cleanup):
    held = []
    closed = []

    async def nested():
        try:
            await asyncio.sleep(0)
        finally:
            closed.append(True)
            if ordinary_cleanup:
                raise ValueError("private-close-error")

    def bad(value, context):
        coroutine = nested()
        coroutine.send(None)
        held.append(coroutine)
        return coroutine

    report = asyncio.run(build(AsyncRuleBinding("r", (), Check(bad))).validate({}))
    assert not report.valid and closed == [True]
    assert "private-close-error" not in json.dumps(report.to_dict())
    with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
        held[0].send(None)


def test_parent_cancel_disarms_future_per_check_timeout_before_cleanup(monkeypatch):
    async def run():
        advance = controlled_clock(monkeypatch)
        entered = asyncio.Event()
        closed = []

        async def check(value, context):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                asyncio.get_running_loop().call_soon(advance)
                for _ in range(3):
                    await asyncio.sleep(0)
                closed.append(True)

        policy = AsyncValidationPolicy(per_validator_timeout_seconds=10, total_timeout_seconds=100)
        task = asyncio.create_task(
            build(AsyncRuleBinding("r", (), AwaitCheck(check)), policy=policy).validate({})
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == [True]

    asyncio.run(run())


def test_total_deadline_does_not_recancel_existing_per_check_timeout_cleanup(monkeypatch):
    from payload_palette import async_validation

    closed = []

    async def run():
        advance = controlled_clock(monkeypatch)
        settling = asyncio.Event()
        original_settle = async_validation._settle

        async def observe_settlement(tasks):
            settling.set()
            return await original_settle(tasks)

        monkeypatch.setattr(async_validation, "_settle", observe_settlement)

        async def check(value, context):
            try:
                asyncio.get_running_loop().call_soon(advance)
                await asyncio.Event().wait()
            finally:
                asyncio.get_running_loop().call_soon(advance)
                await settling.wait()
                await asyncio.sleep(0)
                closed.append(True)

        policy = AsyncValidationPolicy(per_validator_timeout_seconds=10, total_timeout_seconds=30)
        return await build(AsyncRuleBinding("r", (), AwaitCheck(check)), policy=policy).validate({})

    report = asyncio.run(run())
    assert not report.valid and closed == [True]
    assert any(issue.code == "validation_deadline" for issue in report.issues)


def test_reserved_job_snapshot_deadline_counts_no_unentered_callback(monkeypatch):
    from payload_palette import async_validation

    original = async_validation.snapshot_json
    copied = []

    def delayed(value, limits):
        result = original(value, limits)
        copied.append(True)
        loop = asyncio.get_running_loop()
        clock = loop.time
        monkeypatch.setattr(loop, "time", lambda: clock() + 20)
        return result

    monkeypatch.setattr(async_validation, "snapshot_json", delayed)
    report = asyncio.run(
        build(
            AsyncRuleBinding("r", (), Check()),
            policy=AsyncValidationPolicy(total_timeout_seconds=10),
        ).validate({})
    )
    assert not report.valid and report.invocations == 0 and copied == [True]
    assert report.issues[0].code == "validation_deadline"


def test_task_creation_failure_closes_worker_and_settles_prior_tasks(monkeypatch):
    from payload_palette import async_validation

    created = []

    def fail(coroutine):
        created.append(coroutine)
        raise OSError("creation-failed")

    monkeypatch.setattr(async_validation.asyncio, "create_task", fail)
    with pytest.raises(OSError, match="creation-failed"):
        asyncio.run(build(AsyncRuleBinding("r", (), Check())).validate({}))
    with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
        created[0].send(None)


def test_previous_cancellation_count_does_not_cancel_a_new_validation():
    async def run():
        task = asyncio.current_task()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert task.cancelling() == 1
        report = await build(AsyncRuleBinding("r", (), Check())).validate({})
        assert report.valid and task.cancelling() == 1

    asyncio.run(run())


@pytest.mark.parametrize("total", [False, True])
def test_late_synchronous_callback_body_cannot_bypass_deadline(total, monkeypatch):
    def slow(value, context):
        loop = asyncio.get_running_loop()
        clock = loop.time
        monkeypatch.setattr(loop, "time", lambda: clock() + 20)
        return RuleResult(True)

    policy = AsyncValidationPolicy(
        per_validator_timeout_seconds=100 if total else 10,
        total_timeout_seconds=10 if total else 100,
    )
    report = asyncio.run(build(AsyncRuleBinding("r", (), Check(slow)), policy=policy).validate({}))
    assert not report.valid and report.invocations == 1
    assert any(
        issue.code == ("validation_deadline" if total else "validator_timeout")
        for issue in report.issues
    )


def test_total_deadline_includes_output_copy(monkeypatch):
    from payload_palette import OutputReport

    original = OutputReport.output.fget
    copied = []

    def slow_output(report):
        result = original(report)
        copied.append(True)
        loop = asyncio.get_running_loop()
        clock = loop.time
        monkeypatch.setattr(loop, "time", lambda: clock() + 20)
        return result

    monkeypatch.setattr(OutputReport, "output", property(slow_output))
    report = asyncio.run(
        build(
            AsyncRuleBinding("r", (), Check()),
            policy=AsyncValidationPolicy(total_timeout_seconds=10),
        ).validate({})
    )
    assert not report.valid and report.invocations == 0 and copied == [True]


@pytest.mark.parametrize("limit", [None, 0, True, 1.5])
def test_invalid_byte_limit_is_not_a_decode_mode_sentinel(limit):
    with pytest.raises(ValueError):
        asyncio.run(build().validate_json_bytes(b"{}", max_input_bytes=limit))


@pytest.mark.parametrize("unavailable", ["lookup", "noncallable"])
def test_unentered_callback_lookup_failure_is_not_an_invocation(unavailable):
    class Lookup:
        fail = False

        @property
        def check(self):
            if self.fail:
                if unavailable == "lookup":
                    raise RuntimeError("private-getter")
                return 1

            async def valid(value, context):
                return RuleResult(True)

            return valid

    checker = Lookup()
    pipeline = build(AsyncRuleBinding("r", (), checker))
    checker.fail = True
    report = asyncio.run(pipeline.validate({}))
    assert not report.valid and report.invocations == 0
    assert report.issues[0].code == "validator_contract"


@pytest.mark.parametrize("cancellation", ["caller", "per_check", "total"])
@pytest.mark.parametrize("as_fix", [False, True])
def test_cancellation_suppression_releases_malformed_returned_coroutines(
    cancellation, as_fix, monkeypatch
):
    async def run():
        advance = controlled_clock(monkeypatch)
        entered = asyncio.Event()
        held = []

        async def misplaced():
            raise AssertionError("must not execute")

        async def check(value, context):
            try:
                entered.set()
                if cancellation != "caller":
                    asyncio.get_running_loop().call_soon(advance)
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                coroutine = misplaced()
                held.append(coroutine)
                if as_fix:
                    result = RuleResult(False, "bad", "", coroutine)
                    object.__setattr__(result, "valid", 1)
                    return result
                return coroutine

        policy = AsyncValidationPolicy(
            per_validator_timeout_seconds=10 if cancellation == "per_check" else 100,
            total_timeout_seconds=10 if cancellation == "total" else 100,
        )
        task = asyncio.create_task(
            build(AsyncRuleBinding("r", (), AwaitCheck(check)), policy=policy).validate({})
        )
        await entered.wait()
        if cancellation == "caller":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            report = await task
            assert not report.valid
            assert any(
                issue.code
                == ("validator_timeout" if cancellation == "per_check" else "validation_deadline")
                for issue in report.issues
            )
        assert len(held) == 1
        with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
            held[0].send(None)
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(run())


def test_offline_catalog_example():
    from examples.async_semantic_validation import run_example

    result = asyncio.run(run_example())
    assert result["approved"]["valid"] and result["approved"]["invocations"] == 5
    assert not result["rejected"]["valid"] and "output" not in result["rejected"]


@pytest.mark.parametrize(
    "value,limits",
    [
        ({"a": 1}, OutputLimits(max_nodes=1)),
        ({"a": "long"}, OutputLimits(max_characters=2)),
        ({"a": [1]}, OutputLimits(max_depth=1)),
    ],
)
def test_structural_snapshot_budgets_reject_before_async_calls(value, limits):
    calls = []
    pipeline = build(
        AsyncRuleBinding("r", (), Check(lambda v, c: calls.append(True))), limits=limits
    )
    with pytest.raises(OutputContractError):
        asyncio.run(pipeline.validate(value))
    assert not calls


def test_unmodified_wall_clock_timeout_settles_every_callback_that_entered():
    async def run():
        entered, closed = [], []

        async def check(value, context):
            entered.append(True)
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.append(True)

        report = await build(
            AsyncRuleBinding("r", (), AwaitCheck(check)),
            policy=AsyncValidationPolicy(
                per_validator_timeout_seconds=0.02, total_timeout_seconds=1
            ),
        ).validate({})
        assert not report.valid and report.invocations == len(entered)
        assert closed == entered and len(asyncio.all_tasks()) == 1
        assert any(
            issue.code in {"validator_timeout", "validation_deadline"} for issue in report.issues
        )

    asyncio.run(run())


def test_final_outcome_rendering_cannot_approve_after_total_deadline(monkeypatch):
    from payload_palette import async_validation

    original = async_validation.output_path
    rendered = []

    def delayed(path):
        result = original(path)
        rendered.append(True)
        loop = asyncio.get_running_loop()
        clock = loop.time
        monkeypatch.setattr(loop, "time", lambda: clock() + 20)
        return result

    monkeypatch.setattr(async_validation, "output_path", delayed)
    report = asyncio.run(
        build(
            AsyncRuleBinding("r", (), Check()),
            policy=AsyncValidationPolicy(total_timeout_seconds=10),
        ).validate({})
    )
    assert rendered == [True] and report.invocations == 1
    assert not report.valid and report.issues[-1].code == "validation_deadline"
