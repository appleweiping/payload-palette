from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress

import pytest
from tests._async_generation_helpers import AwaitCheck, Check, runner

from payload_palette import (
    AsyncGenerationRunner,
    AsyncValidationPolicy,
    GeneratedResponse,
    GenerationPolicy,
    OutputContractError,
    OutputLimits,
    PayloadValidationError,
    RuleResult,
    TokenUsage,
    ValidationIssue,
    async_validation,
)


@pytest.mark.parametrize("owner", ["check", "validation", "generation", "tie"])
@pytest.mark.parametrize("swallow", [False, True])
def test_one_deadline_owner_settles_callbacks_and_cannot_accept_late(owner, swallow, monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        now = [loop.time()]
        monkeypatch.setattr(loop, "time", lambda: now[0])
        entered, cleaning, release = (asyncio.Event() for _ in range(3))
        closed = []

        async def check(value, context):
            try:
                entered.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow:
                    raise
                return RuleResult(True)
            finally:
                cleaning.set()
                await release.wait()
                closed.append(True)

        policy = AsyncValidationPolicy(
            per_validator_timeout_seconds=10 if owner == "check" else 100,
            total_timeout_seconds=10 if owner in {"validation", "tie"} else 100,
        )
        instance, provider = runner(
            AwaitCheck(check),
            async_policy=policy,
            policy=GenerationPolicy(
                total_timeout_seconds=10 if owner in {"generation", "tie"} else 200
            ),
        )
        task = asyncio.create_task(instance.run("offline"))
        await entered.wait()
        now[0] += 11
        await cleaning.wait()
        assert not task.done() and not closed and len(provider.requests) == 1
        release.set()
        report = await task
        assert closed == [True] and len(asyncio.all_tasks()) == 1
        assert report.termination == (
            "deadline" if owner in {"generation", "tie"} else "validator_error"
        )
        assert report.attempts[0].status == (
            "timeout" if owner in {"generation", "tie"} else "validator_error"
        )
        assert report.validator_invocations == 1 and report.usage_complete
        assert report.output is None

    asyncio.run(scenario())


@pytest.mark.parametrize("earlier", ["check", "validation"])
def test_cleanup_crossing_generation_deadline_overrides_earlier_local_error(earlier, monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        now = [loop.time()]
        monkeypatch.setattr(loop, "time", lambda: now[0])
        entered, cleaning, release = (asyncio.Event() for _ in range(3))
        cancelled, closed = [], []

        async def check(value, context):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cancelled.append(asyncio.current_task().cancelling())
                cleaning.set()
                await release.wait()
                closed.append(True)

        instance, provider = runner(
            AwaitCheck(check),
            async_policy=AsyncValidationPolicy(
                per_validator_timeout_seconds=10 if earlier == "check" else 100,
                total_timeout_seconds=10 if earlier == "validation" else 100,
            ),
            policy=GenerationPolicy(total_timeout_seconds=20),
        )
        task = asyncio.create_task(instance.run("offline"))
        await entered.wait()
        now[0] += 11
        await cleaning.wait()
        now[0] += 10
        for _ in range(3):
            await asyncio.sleep(0)
        assert not task.done() and not closed
        release.set()
        report = await task
        assert cancelled == [1] and closed == [True]
        assert report.termination == "deadline" and report.attempts[0].status == "timeout"
        assert report.validator_invocations == 1 and len(provider.requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("owner", ["generation", "validation", "tie"])
def test_early_timer_provenance_survives_clock_before_nominal_deadline(owner, monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "time", lambda: 100.0)
        closed = []

        async def check(value, context):
            try:
                # Only enlarge timer resolution after provider completion, so
                # this exercises the validation owner, not the provider timer.
                monkeypatch.setattr(loop, "_clock_resolution", 3.0)
                await asyncio.sleep(0)
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        instance, _ = runner(
            AwaitCheck(check),
            async_policy=AsyncValidationPolicy(
                per_validator_timeout_seconds=100,
                total_timeout_seconds=1 if owner in {"validation", "tie"} else 2,
            ),
            policy=GenerationPolicy(
                total_timeout_seconds=1 if owner in {"generation", "tie"} else 2
            ),
        )
        report = await instance.run("offline")
        assert loop.time() == 100 and closed == [True]
        assert report.termination == ("validator_error" if owner == "validation" else "deadline")
        assert report.validator_invocations == 1 and report.usage_complete

    asyncio.run(scenario())


def test_provider_attempt_timeout_does_not_shrink_validation_deadline(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        now = [loop.time()]
        monkeypatch.setattr(loop, "time", lambda: now[0])

        async def check(value, context):
            now[0] += 2
            await asyncio.sleep(0)
            return RuleResult(True)

        instance, _ = runner(
            AwaitCheck(check),
            policy=GenerationPolicy(attempt_timeout_seconds=1, total_timeout_seconds=100),
        )
        report = await instance.run("offline")
        assert report.valid

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["decode", "snapshot"])
@pytest.mark.parametrize("late", [False, True])
def test_structural_exception_paths_observe_outer_deadline_before_reask(phase, late, monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        now = [loop.time()]
        monkeypatch.setattr(loop, "time", lambda: now[0])
        original = async_validation.decode_json_bytes

        def decode(*args, **kwargs):
            if late:
                now[0] += 11
            return original(*args, **kwargs)

        monkeypatch.setattr(async_validation, "decode_json_bytes", decode)
        payload = "{" if phase == "decode" else '"too long"'
        instance, provider = runner(
            Check(),
            responses=(payload,),
            limits=OutputLimits(max_characters=1),
            policy=GenerationPolicy(max_attempts=1, total_timeout_seconds=10),
        )
        report = await instance.run("offline")
        assert report.termination == ("deadline" if late else "attempts_exhausted")
        assert report.attempts[0].status == ("timeout" if late else "invalid_output")
        assert report.validator_invocations == 0 and len(provider.requests) == 1

    asyncio.run(scenario())


def test_caller_repeated_cancel_drains_owned_children_without_second_cleanup_cancel():
    async def scenario():
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

        instance, provider = runner(AwaitCheck(check))
        task = asyncio.create_task(instance.run("offline"))
        await entered.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelling() == 2 and finished == [True]
        assert len(provider.requests) == 1 and len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("cleanup_control", [None, KeyboardInterrupt, SystemExit])
def test_genuine_control_identity_wins_after_all_sibling_cleanup(control, cleanup_control):
    async def scenario():
        primary = control("original")
        entered = asyncio.Event()
        closed = []

        async def fail(value, context):
            await entered.wait()
            raise primary

        async def sibling(value, context):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.append(True)
                if cleanup_control is not None:
                    raise cleanup_control("cleanup")

        instance, provider = runner(AwaitCheck(fail), AwaitCheck(sibling))
        with pytest.raises(control) as caught:
            await instance.run("offline")
        assert caught.value is primary and closed == [True]
        assert len(provider.requests) == 1 and len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "control", [OSError, OutputContractError, PayloadValidationError, KeyboardInterrupt, SystemExit]
)
def test_later_task_creation_failure_settles_already_entered_sibling(control, monkeypatch):
    async def scenario():
        primary = (
            control((ValidationIssue("internal", "create failed"),))
            if control is PayloadValidationError
            else control("create failed")
        )
        entered, closed = asyncio.Event(), []
        created = []
        original = asyncio.create_task

        async def first(value, context):
            await entered.wait()
            return RuleResult(True)

        async def second(value, context):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                closed.append(True)

        def create(coroutine):
            created.append(coroutine)
            if len(created) == 3:
                assert entered.is_set()
                raise primary
            return original(coroutine)

        monkeypatch.setattr(async_validation.asyncio, "create_task", create)
        instance, provider = runner(
            AwaitCheck(first),
            AwaitCheck(second),
            Check(),
            async_policy=AsyncValidationPolicy(max_concurrency=2),
        )
        with pytest.raises(control) as caught:
            await instance.run("offline")
        assert caught.value is primary and closed == [True] and len(created) == 3
        assert all(inspect.getcoroutinestate(c) == inspect.CORO_CLOSED for c in created)
        assert len(provider.requests) == 1 and len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["future", "task"])
def test_borrowed_resources_are_not_awaited_or_cancelled_by_generation(kind):
    async def scenario():
        resource = (
            asyncio.get_running_loop().create_future()
            if kind == "future"
            else asyncio.create_task(asyncio.Event().wait())
        )
        try:
            instance, _ = runner(Check(lambda v, c: resource))
            report = await instance.run("offline")
            assert report.termination == "validator_error"
            assert not resource.done()
            if kind == "task":
                assert resource.cancelling() == 0
        finally:
            resource.cancel()
            with suppress(asyncio.CancelledError):
                await resource

    asyncio.run(scenario())


def test_cancel_before_provider_admission_never_calls_provider():
    async def scenario():
        instance, provider = runner(Check())
        task = asyncio.create_task(instance.run("offline"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not provider.requests

    asyncio.run(scenario())


def test_provider_cancellation_after_response_cannot_enter_validation():
    async def scenario():
        calls = []
        instance, _ = runner(Check(lambda v, c: calls.append(v)))

        class Provider:
            async def generate(self, request):
                asyncio.current_task().cancel()
                return GeneratedResponse('"yes"', TokenUsage(2, 1))

        with pytest.raises(asyncio.CancelledError):
            await AsyncGenerationRunner(instance.pipeline, Provider()).run("offline")
        # Drain the pending injection without changing the old runner's policy.
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(0)
        assert not calls

    asyncio.run(scenario())
