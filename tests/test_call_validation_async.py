import asyncio
import inspect
from contextlib import suppress

import pytest

from payload_palette import CallAdapter, OutputContractError, OutputLimits, PayloadValidationError


def test_async_laziness_direct_caller_task_and_identity():
    async def scenario():
        entries = []
        owner = asyncio.current_task()

        async def target(value):
            entries.append(asyncio.current_task())
            await asyncio.sleep(0)
            return value

        value = [1]
        adapter = CallAdapter(
            target, annotations={"value": list[int], "return": list[int]}, validate_return=True
        )
        pending = adapter.call_async(value)
        assert entries == []
        assert await pending is value
        assert entries == [owner]

    asyncio.run(scenario())


def test_execution_mode_mismatch_does_not_create_or_enter_target():
    entries = []

    async def asynchronous():
        entries.append(1)

    def synchronous():
        entries.append(2)

    with pytest.raises(OutputContractError):
        CallAdapter(asynchronous, annotations={}).call()
    with pytest.raises(OutputContractError):
        asyncio.run(CallAdapter(synchronous, annotations={}).call_async())
    assert entries == []
    if hasattr(inspect, "markcoroutinefunction"):
        inspect.markcoroutinefunction(synchronous)
        assert CallAdapter(synchronous, annotations={}).call() is None
        assert entries == [2]


def test_pending_self_cancellation_precedes_target_and_prior_caught_cancel_is_valid():
    async def scenario():
        entries = []

        async def target(value):
            entries.append(value)
            return value

        adapter = CallAdapter(target, annotations={"value": int})

        async def pending():
            asyncio.current_task().cancel()
            await adapter.call_async(7)

        task = asyncio.create_task(pending())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert entries == []

        async def prior():
            current = asyncio.current_task()
            current.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.sleep(0)
            assert current.cancelling() == 1
            return await adapter.call_async(9)

        assert await asyncio.create_task(prior()) == 9
        assert entries == [9]

    asyncio.run(scenario())


@pytest.mark.parametrize("suppress", [False, True])
def test_cancellation_during_target_uses_ordinary_direct_await_semantics(suppress):
    async def scenario():
        entered = asyncio.Event()
        finished = []

        async def target():
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                if not suppress:
                    raise
                return 3
            finally:
                finished.append(1)

        adapter = CallAdapter(target, annotations={"return": int}, validate_return=True)
        task = asyncio.create_task(adapter.call_async())
        await entered.wait()
        task.cancel()
        if suppress:
            assert await task == 3
        else:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished == [1]

    asyncio.run(scenario())


def test_concurrent_calls_have_distinct_aggregate_budgets():
    async def scenario():
        async def target(value):
            await asyncio.sleep(0)
            return value

        adapter = CallAdapter(
            target,
            annotations={"value": int, "return": int},
            validate_return=True,
            limits=OutputLimits(max_nodes=3, max_schema_steps=3),
        )
        assert await asyncio.gather(*(adapter.call_async(n) for n in range(20))) == list(range(20))

    asyncio.run(scenario())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_invalid_fresh_native_coroutine_result_is_closed_without_body(asynchronous):
    async def scenario():
        bodies = []
        produced = []

        async def nested():
            bodies.append(1)

        def sync_target():
            result = nested()
            produced.append(result)
            return result

        async def async_target():
            return sync_target()

        adapter = CallAdapter(
            async_target if asynchronous else sync_target,
            annotations={"return": int},
            validate_return=True,
        )
        with pytest.raises(PayloadValidationError):
            if asynchronous:
                await adapter.call_async()
            else:
                adapter.call()
        assert inspect.getcoroutinestate(produced[0]) == inspect.CORO_CLOSED
        assert bodies == []

    asyncio.run(scenario())


def test_borrowed_async_resources_are_not_cancelled_advanced_or_closed():
    async def scenario():
        events = []

        class Awaitable:
            def __await__(self):
                events.append("await")
                yield

        async def sleeper():
            await asyncio.sleep(0)
            return 1

        started = sleeper()
        started.send(None)
        task = asyncio.create_task(sleeper())
        future = asyncio.Future()

        def returning(result):
            def target():
                return result

            return target

        try:
            for result in [started, task, future, Awaitable()]:
                target = returning(result)
                with pytest.raises(PayloadValidationError):
                    CallAdapter(target, annotations={"return": int}, validate_return=True).call()
                assert CallAdapter(target, annotations={}).call() is result
            assert inspect.getcoroutinestate(started) == inspect.CORO_SUSPENDED
            assert not task.cancelled() and not future.cancelled() and events == []
            assert await task == 1
        finally:
            started.close()
            if not task.done():
                task.cancel()
            if not future.done():
                future.cancel()

    asyncio.run(scenario())


def test_unvalidated_fresh_coroutine_is_caller_owned():
    async def nested():
        return 3

    def target():
        return nested()

    result = CallAdapter(target, annotations={}).call()
    assert inspect.getcoroutinestate(result) == inspect.CORO_CREATED
    assert asyncio.run(result) == 3


def test_async_target_exception_and_invalid_argument_have_separate_boundaries():
    async def scenario():
        entries = []
        failure = TypeError("actual target body")

        async def target(value):
            entries.append(value)
            raise failure

        adapter = CallAdapter(target, annotations={"value": int})
        with pytest.raises(PayloadValidationError):
            await adapter.call_async(True)
        assert entries == []
        with pytest.raises(TypeError) as caught:
            await adapter.call_async(1)
        assert caught.value is failure and entries == [1]

    asyncio.run(scenario())
