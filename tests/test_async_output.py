from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import replace

import pytest
from examples.async_model_output import run_example

from payload_palette import (
    AsyncOutputPolicy,
    IncrementalJSONError,
    IncrementalLimits,
    OutputSchema,
    RuleBinding,
    RuleResult,
    ValidationPipeline,
    validate_async_output_chunks,
)


def pipeline():
    return ValidationPipeline(OutputSchema("object", additional_properties=True))


class Source:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pulls = self.closes = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.pulls += 1
        await asyncio.sleep(0)
        try:
            result = next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration from None
        if isinstance(result, BaseException):
            raise result
        return result

    async def aclose(self):
        self.closes += 1


class Callback:
    def __init__(self, callback):
        self.callback = callback

    def check(self, value, context):
        return self.callback(value, context)


def observing_pipeline(callback):
    return ValidationPipeline(
        OutputSchema("object", additional_properties=True),
        (RuleBinding("observe", (), Callback(callback)),),
    )


def assert_coroutines_closed(values):
    for value in values:
        # CPython 3.12.0 can retain cr_frame (and report CORO_CREATED) even
        # after close. The observable reuse prohibition is portable instead.
        with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
            value.send(None)


@pytest.mark.parametrize("width", [1, 2, 3, 5, 13, 1000])
@pytest.mark.parametrize("escaped", [False, True])
def test_real_chunk_boundaries_and_one_final_semantic_pass(width, escaped):
    value = {"a": [1, None, True, {"unicode": "你好𝄞\n"}], "empty": {}}
    raw = json.dumps(value, ensure_ascii=escaped).encode()
    source = Source([raw[i : i + width] for i in range(0, len(raw), width)])
    events = []

    async def observe(progress):
        events.extend(progress.events)
        assert source.closes == 0
        await asyncio.sleep(0)

    result = asyncio.run(
        validate_async_output_chunks(source, pipeline(), on_progress=observe, close_iterator=True)
    )
    assert result.report.valid and result.report.output == value
    assert events[-1].value.to_python() == value
    assert source.closes == 1 and source.pulls == (len(raw) + width - 1) // width + 1


def test_observer_backpressure_no_prefetch_and_cleanup_before_validation():
    async def scenario():
        source = Source([b'{"a":', b"1}"])
        observed, allow = asyncio.Event(), asyncio.Event()
        seen = []

        async def observe(progress):
            seen.append(progress.statistics)
            if len(seen) == 1:
                observed.set()
                await allow.wait()

        def check(value, context):
            assert source.closes == 1
            return RuleResult(True)

        job = asyncio.create_task(
            validate_async_output_chunks(
                source, observing_pipeline(check), on_progress=observe, close_iterator=True
            )
        )
        await observed.wait()
        for _ in range(5):
            await asyncio.sleep(0)
        assert source.pulls == 1 and not job.done()
        allow.set()
        result = await job
        assert result.report.valid and source.closes == 1
        assert len(seen) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("owned", [False, True])
def test_ownership_defaults_and_finite_eof(owned):
    source = Source([b"{}"])
    result = asyncio.run(validate_async_output_chunks(source, pipeline(), close_iterator=owned))
    assert result.report.valid and source.closes == int(owned) and source.pulls == 2


@pytest.mark.parametrize("raw", [b"{", b"{} {}", b'{"a":1,"a":2}', b"\xff", b'{"a":NaN}'])
def test_syntax_failure_closes_and_never_runs_semantic_callback(raw):
    source = Source([raw])

    def forbidden(value, context):
        raise AssertionError("semantic validation ran on invalid syntax")

    with pytest.raises(IncrementalJSONError):
        asyncio.run(
            validate_async_output_chunks(source, observing_pipeline(forbidden), close_iterator=True)
        )
    assert source.closes == 1


def test_schema_rejection_is_a_final_report():
    source = Source([b"{}"])
    result = asyncio.run(
        validate_async_output_chunks(
            source, ValidationPipeline(OutputSchema("integer")), close_iterator=True
        )
    )
    assert not result.report.valid and source.closes == 1


@pytest.mark.parametrize("failure", [RuntimeError("source"), asyncio.CancelledError("source")])
def test_source_failure_preserved_and_no_extra_pull(failure):
    source = Source([b"{}", failure, b"never consumed"])

    async def scenario():
        with pytest.raises(type(failure)) as caught:
            await validate_async_output_chunks(source, pipeline(), close_iterator=True)
        assert caught.value is failure

    asyncio.run(scenario())
    assert source.pulls == 2 and source.closes == 1


@pytest.mark.parametrize("phase", ["source", "observer", "cleanup"])
def test_real_external_cancel_waits_for_owned_cleanup_without_hidden_tasks(phase):
    async def scenario():
        entered = asyncio.Event()
        never = asyncio.Event()

        class Waiting(Source):
            async def __anext__(self):
                if phase == "source":
                    entered.set()
                    await never.wait()
                return await super().__anext__()

            async def aclose(self):
                self.closes += 1
                if phase == "cleanup":
                    entered.set()
                    await never.wait()

        async def observe(progress):
            if phase == "observer":
                entered.set()
                await never.wait()

        source = Waiting([b"{}"])
        job = asyncio.create_task(
            validate_async_output_chunks(
                source, pipeline(), on_progress=observe, close_iterator=True
            )
        )
        await entered.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert source.closes == 1
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["source", "observer", "cleanup"])
def test_swallowed_external_cancel_cannot_approve(phase):
    async def scenario():
        entered, never = asyncio.Event(), asyncio.Event()

        async def swallow():
            entered.set()
            with suppress(asyncio.CancelledError):
                await never.wait()

        class Swallow(Source):
            async def __anext__(self):
                if phase == "source" and self.pulls == 0:
                    await swallow()
                return await super().__anext__()

            async def aclose(self):
                self.closes += 1
                if phase == "cleanup":
                    await swallow()

        async def observe(progress):
            if phase == "observer":
                await swallow()

        source = Swallow([b"{}"])
        job = asyncio.create_task(
            validate_async_output_chunks(
                source, pipeline(), on_progress=observe, close_iterator=True
            )
        )
        await entered.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert source.closes == 1
        assert job.cancelling() == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["source", "observer", "cleanup"])
@pytest.mark.parametrize("swallow", [False, True])
def test_actual_cooperative_timeouts_and_swallowing(phase, swallow):
    async def scenario():
        async def wait():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if not swallow:
                    raise

        class Slow(Source):
            async def __anext__(self):
                if phase == "source" and self.pulls == 0:
                    await wait()
                return await super().__anext__()

            async def aclose(self):
                self.closes += 1
                if phase == "cleanup":
                    await wait()

        async def observe(progress):
            if phase == "observer":
                await wait()

        source = Slow([b"{}"])
        options = AsyncOutputPolicy(0.03 if phase != "cleanup" else 10, 0.03)
        with pytest.raises(TimeoutError):
            await validate_async_output_chunks(
                source, pipeline(), policy=options, on_progress=observe, close_iterator=True
            )
        assert source.closes == 1
        assert asyncio.current_task().cancelling() == 0
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "primary", [None, RuntimeError("primary"), asyncio.CancelledError("primary")]
)
@pytest.mark.parametrize("where", ["lookup", "call", "await"])
def test_cleanup_ordinary_failure_preserves_primary_or_blocks_approval(primary, where):
    cleanup = OSError("cleanup")

    class Failing(Source):
        @property
        def aclose(self):
            if where == "lookup":
                raise cleanup

            def method():
                if where == "call":
                    raise cleanup

                async def fail():
                    raise cleanup

                return fail()

            return method

    source = Failing([b"{}"] if primary is None else [primary])
    expected = cleanup if primary is None else primary

    async def scenario():
        with pytest.raises(type(expected)) as caught:
            await validate_async_output_chunks(source, pipeline(), close_iterator=True)
        assert caught.value is expected
        if primary is not None:
            assert "cleanup also failed" in primary.__notes__[-1]

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["observer", "cleanup"])
@pytest.mark.parametrize("malformed", ["synchronous", "wrong_result", "async_generator"])
def test_malformed_awaitables_never_approve_or_warn(phase, malformed):
    def callback(*args):
        if malformed == "synchronous":
            return None
        if malformed == "async_generator":

            async def generator():
                yield None

            return generator()

        async def wrong():
            return 1

        return wrong()

    source = Source([b"{}"])
    if phase == "cleanup":
        source.aclose = callback
    with pytest.raises(TypeError):
        asyncio.run(
            validate_async_output_chunks(
                source,
                pipeline(),
                close_iterator=True,
                on_progress=callback if phase == "observer" else None,
            )
        )


@pytest.mark.parametrize("field", ["timeout_seconds", "cleanup_timeout_seconds"])
@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), 86401, "1", 10**1000])
def test_policy_strict_finite_bounds(field, value):
    with pytest.raises(ValueError):
        replace(AsyncOutputPolicy(), **{field: value})


@pytest.mark.parametrize("kwargs", [{"policy": {}}, {"close_iterator": 1}, {"on_progress": 1}])
def test_invalid_configuration_precedes_iterator_creation(kwargs):
    class Forbidden:
        def __aiter__(self):
            raise AssertionError("source inspected before validation")

    with pytest.raises(ValueError):
        asyncio.run(validate_async_output_chunks(Forbidden(), pipeline(), **kwargs))


def test_iterator_creation_failure_is_propagated_without_claiming_ownership():
    failure = RuntimeError("creation failed")

    class Broken:
        def __aiter__(self):
            raise failure

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(validate_async_output_chunks(Broken(), pipeline(), close_iterator=True))
    assert caught.value is failure


def test_parser_limits_prevent_extra_pull():
    source = Source([b"{", b"}", b"not pulled"])
    with pytest.raises(IncrementalJSONError):
        asyncio.run(
            validate_async_output_chunks(
                source, pipeline(), limits=IncrementalLimits(max_chunks=1), close_iterator=True
            )
        )
    assert source.pulls == 2 and source.closes == 1


def test_iterator_without_aclose_and_empty_number_final_events():
    class NoClose:
        def __init__(self):
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return b"123"

    result = asyncio.run(
        validate_async_output_chunks(
            NoClose(), ValidationPipeline(OutputSchema("integer")), close_iterator=True
        )
    )
    assert result.report.output == 123
    assert len(result.final_events) == 1 and result.final_events[0].value.to_python() == 123


@pytest.mark.parametrize("phase", ["source", "observer", "cleanup", "validator"])
def test_synchronous_trusted_work_past_deadline_cannot_approve(monkeypatch, phase):
    async def scenario():
        loop = asyncio.get_running_loop()
        real_time = loop.time
        offset = 0

        def exceed():
            nonlocal offset
            offset = 1000

        class Blocking(Source):
            async def __anext__(self):
                if phase == "source":
                    exceed()
                    return b"{}"
                return await super().__anext__()

            async def aclose(self):
                self.closes += 1
                if phase == "cleanup":
                    exceed()

        async def observe(progress):
            if phase == "observer":
                exceed()

        def validator(value, context):
            if phase == "validator":
                exceed()
            return RuleResult(True)

        source = Blocking([b"{}"])
        with monkeypatch.context() as patch:
            patch.setattr(loop, "time", lambda: real_time() + offset)
            with pytest.raises(TimeoutError):
                await validate_async_output_chunks(
                    source,
                    observing_pipeline(validator),
                    policy=AsyncOutputPolicy(100, 100),
                    on_progress=observe,
                    close_iterator=True,
                )
        assert source.closes == 1

    asyncio.run(scenario())


def test_early_timeout_delivery_swallowed_as_eof_still_fails(monkeypatch):
    async def scenario():
        real_timeout = asyncio.timeout_at

        class Early(Source):
            async def __anext__(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise StopAsyncIteration from None

        def early(deadline):
            # Model a loop's early timer delivery independently of host speed.
            return real_timeout(asyncio.get_running_loop().time() - 1)

        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "timeout_at", early)
            with pytest.raises(TimeoutError):
                await validate_async_output_chunks(Early([]), pipeline())
        assert asyncio.current_task().cancelling() == 0

    asyncio.run(scenario())


def test_async_generator_source_is_closed_and_no_lookahead_after_observer_error():
    async def scenario():
        closed = []

        async def source():
            try:
                yield b"{}"
                raise AssertionError("prefetch is forbidden")
            finally:
                closed.append(True)

        failure = RuntimeError("observer")

        async def observe(progress):
            raise failure

        with pytest.raises(RuntimeError) as caught:
            await validate_async_output_chunks(
                source(), pipeline(), on_progress=observe, close_iterator=True
            )
        assert caught.value is failure and closed == [True]

    asyncio.run(scenario())


def test_new_cleanup_control_exception_is_not_hidden_by_primary():
    async def scenario():
        primary, control = ValueError("primary"), asyncio.CancelledError("cleanup control")

        class Interrupted(Source):
            async def aclose(self):
                raise control

        with pytest.raises(asyncio.CancelledError) as caught:
            await validate_async_output_chunks(
                Interrupted([primary]), pipeline(), close_iterator=True
            )
        assert caught.value is control

    asyncio.run(scenario())


def test_prior_handled_cancellation_count_does_not_poison_a_new_consumption():
    async def scenario():
        task = asyncio.current_task()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(0)
        before = task.cancelling()
        result = await validate_async_output_chunks(
            Source([b"{}"]), pipeline(), close_iterator=True
        )
        assert result.report.valid and task.cancelling() == before == 1

    asyncio.run(scenario())


def test_independent_concurrent_consumers_do_not_share_state():
    async def scenario():
        jobs = [
            validate_async_output_chunks(
                Source([b'{"index":', str(i).encode(), b"}"]), pipeline(), close_iterator=True
            )
            for i in range(20)
        ]
        reports = await asyncio.gather(*jobs)
        assert [report.report.output for report in reports] == [{"index": i} for i in range(20)]
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(scenario())


def test_offline_example():
    result = asyncio.run(run_example())
    assert result["valid"] and result["closed"]
    assert result["output"] == {"label": "星空𝄞", "confidence": 0.9}
    assert result["observations"] > 1 and result["values"] == 3


@pytest.mark.parametrize("control", [KeyboardInterrupt("interrupted"), SystemExit(9)])
def test_process_controls_preserved_through_ordinary_cleanup_failure(control):
    async def scenario():
        class BrokenClose(Source):
            async def aclose(self):
                raise OSError("cleanup")

        with pytest.raises(type(control)) as caught:
            await validate_async_output_chunks(
                BrokenClose([control]), pipeline(), close_iterator=True
            )
        assert caught.value is control and "cleanup also failed" in control.__notes__[-1]

    asyncio.run(scenario())


def test_missing_task_guard_does_not_touch_source(monkeypatch):
    async def scenario():
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "current_task", lambda: None)
            source = Source([b"{}"])
            with pytest.raises(RuntimeError, match="requires an asyncio task"):
                await validate_async_output_chunks(source, pipeline())
            assert source.pulls == 0

    asyncio.run(scenario())


def test_early_cleanup_timeout_is_detected_even_if_adapter_clears_cancel_count(monkeypatch):
    async def scenario():
        real_timeout = asyncio.timeout_at
        calls = 0

        def early_close(deadline):
            nonlocal calls
            calls += 1
            return real_timeout(asyncio.get_running_loop().time() - 1 if calls == 2 else deadline)

        class Manipulating(Source):
            async def aclose(self):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # This violates the documented adapter contract; the expired
                    # timeout guard still must not turn it into an approval.
                    asyncio.current_task().uncancel()

        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "timeout_at", early_close)
            with pytest.raises(TimeoutError, match="cleanup deadline"):
                await validate_async_output_chunks(
                    Manipulating([b"{}"]), pipeline(), close_iterator=True
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["observer", "cleanup"])
def test_malformed_nested_coroutine_result_is_closed_without_running(phase):
    nested = []

    async def forbidden():
        raise AssertionError("nested coroutine must not run")

    async def malformed(*args):
        value = forbidden()
        nested.append(value)
        return value

    source = Source([b"{}"])
    if phase == "cleanup":
        source.aclose = malformed
    with pytest.raises(TypeError, match="resolve to None"):
        asyncio.run(
            validate_async_output_chunks(
                source,
                pipeline(),
                on_progress=malformed if phase == "observer" else None,
                close_iterator=True,
            )
        )
    assert len(nested) == 1
    assert_coroutines_closed(nested)


@pytest.mark.parametrize("phase", ["source", "observer", "cleanup"])
def test_translated_timeout_cancellation_keeps_timeout_classification(phase):
    async def scenario():
        translated = RuntimeError("adapter translated cancellation")

        async def translate():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise translated from None

        class Translating(Source):
            async def __anext__(self):
                if phase == "source":
                    await translate()
                return await super().__anext__()

            async def aclose(self):
                self.closes += 1
                if phase == "cleanup":
                    await translate()

        async def observe(progress):
            if phase == "observer":
                await translate()

        source = Translating([b"{}"])
        with pytest.raises(TimeoutError) as caught:
            await validate_async_output_chunks(
                source,
                pipeline(),
                policy=AsyncOutputPolicy(10 if phase == "cleanup" else 0.03, 0.03),
                on_progress=observe,
                close_iterator=True,
            )
        assert caught.value.__cause__ is translated
        assert source.closes == 1 and asyncio.current_task().cancelling() == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["async_aiter", "sync_aiter", "chunk"])
def test_malformed_source_coroutines_are_closed_without_warning(phase):
    created = []

    async def forbidden():
        raise AssertionError("malformed coroutine must not be awaited")

    class Bad:
        def __aiter__(self):
            value = forbidden()
            created.append(value)
            return value

    class AsyncBad:
        async def __aiter__(self):
            raise AssertionError("malformed __aiter__ must not run")

    if phase == "async_aiter":
        source = AsyncBad()
    elif phase == "sync_aiter":
        source = Bad()
    else:
        value = forbidden()
        created.append(value)
        source = Source([value])
    with pytest.raises((TypeError, ValueError)):
        asyncio.run(validate_async_output_chunks(source, pipeline(), close_iterator=True))
    assert_coroutines_closed(created)


@pytest.mark.parametrize(
    "style", ["inherited", "static", "class", "descriptor", "callable", "property"]
)
def test_iterator_lookup_preserves_class_descriptor_and_shadow_semantics(style):
    target = Source([b"{}"])

    def bad_shadow():
        raise AssertionError("instance __aiter__ shadow must be ignored")

    class Base:
        def __aiter__(self):
            return target

    class Inherited(Base):
        pass

    class Static:
        @staticmethod
        def __aiter__():
            return target

    class Class:
        @classmethod
        def __aiter__(cls):
            assert cls is Class
            return target

    class IteratorDescriptor:
        def __get__(self, instance, cls):
            assert type(instance) is cls is Custom
            return lambda: target

    class Custom:
        __aiter__ = IteratorDescriptor()

    class Callable:
        def __call__(self):
            return target

    class Bare:
        __aiter__ = Callable()

    class Property:
        @property
        def __aiter__(self):
            return lambda: target

    source = {
        "inherited": Inherited,
        "static": Static,
        "class": Class,
        "descriptor": Custom,
        "callable": Bare,
        "property": Property,
    }[style]()
    source.__dict__["__aiter__"] = bad_shadow
    result = asyncio.run(validate_async_output_chunks(source, pipeline(), close_iterator=True))
    assert result.report.valid and target.closes == 1


def test_instance_only_iterator_method_is_not_a_valid_async_protocol():
    class Missing:
        pass

    source = Missing()
    source.__aiter__ = lambda: Source([b"{}"])
    with pytest.raises(TypeError, match="__aiter__"):
        asyncio.run(validate_async_output_chunks(source, pipeline()))


@pytest.mark.parametrize("phase", ["source", "observer", "cleanup"])
@pytest.mark.parametrize("returned", ["translate", "coroutine"])
def test_external_cancel_with_malformed_result_or_translation_remains_cancellation(phase, returned):
    async def scenario():
        entered = asyncio.Event()
        inner = []

        async def forbidden():
            raise AssertionError("returned coroutine must not execute")

        async def malformed_after_cancel():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if returned == "translate":
                    raise RuntimeError("translated cancellation") from None
                value = forbidden()
                inner.append(value)
                return value

        class Broken(Source):
            async def __anext__(self):
                if phase == "source":
                    return await malformed_after_cancel()
                return await super().__anext__()

            async def aclose(self):
                self.closes += 1
                if phase == "cleanup":
                    return await malformed_after_cancel()

        async def observe(progress):
            if phase == "observer":
                return await malformed_after_cancel()

        source = Broken([b"{}"])
        job = asyncio.create_task(
            validate_async_output_chunks(
                source, pipeline(), on_progress=observe, close_iterator=True
            )
        )
        await entered.wait()
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert source.closes == 1 and job.cancelling() == 1
        assert_coroutines_closed(inner)

    asyncio.run(scenario())


@pytest.mark.parametrize("close_fails", [False, True])
def test_original_external_cancellation_message_is_retained(close_fails):
    async def scenario():
        entered = asyncio.Event()

        class Cancelled(Source):
            async def __anext__(self):
                entered.set()
                await asyncio.Event().wait()

            async def aclose(self):
                self.closes += 1
                if close_fails:
                    raise OSError("close failed")

        source = Cancelled([])
        job = asyncio.create_task(
            validate_async_output_chunks(source, pipeline(), close_iterator=True)
        )
        await entered.wait()
        job.cancel("caller cancellation reason")
        with pytest.raises(asyncio.CancelledError) as caught:
            await job
        assert caught.value.args == ("caller cancellation reason",)
        assert source.closes == 1 and job.cancelling() == 1

    asyncio.run(scenario())


def test_descriptor_binding_bypasses_metaclass_attribute_interception():
    calls = []

    class Meta(type):
        def __getattribute__(cls, name):
            if name == "__get__":
                calls.append("intercepted")
                return lambda descriptor, obj, typ: lambda: Source([b'{"value":2}'])
            return super().__getattribute__(name)

    class Descriptor(metaclass=Meta):
        def __get__(self, obj, typ):
            return lambda: Source([b'{"value":1}'])

    class Custom:
        __aiter__ = Descriptor()

    async def scenario():
        ordinary = await anext(aiter(Custom()))
        result = await validate_async_output_chunks(Custom(), pipeline(), close_iterator=True)
        assert result.report.output == json.loads(ordinary) == {"value": 1}

    asyncio.run(scenario())
    assert calls == []
