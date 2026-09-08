"""Backpressured asynchronous consumption of strict structured-output bytes."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

from .incremental_output import (
    IncrementalLimits,
    IncrementalOutputSession,
    IncrementalProgress,
    IncrementalResult,
)
from .output_validation import ValidationPipeline


@dataclass(frozen=True, slots=True)
class AsyncOutputPolicy:
    """Cooperative consumption deadline and a separate owned-cleanup grace.

    The deadline covers iteration, observation and final synchronous validation.
    Cleanup can use its own grace after this deadline. Trusted code that blocks
    the event loop or suppresses cancellation cannot be forcibly preempted.
    """

    timeout_seconds: float = 30.0
    cleanup_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in ("timeout_seconds", "cleanup_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= 86_400:
                raise ValueError(f"{name} must be finite and between zero and 86400 seconds")


def _cancelled(task: asyncio.Task[Any], initial: int) -> None:
    # Do not let a trusted source/observer turn swallowed cancellation into an
    # approved result. Never change the caller's cancellation count ourselves.
    if task.cancelling() > initial:
        raise asyncio.CancelledError


def _deadline(loop: asyncio.AbstractEventLoop, deadline: float) -> None:
    # The loop timer may not run while synchronous trusted code is executing.
    if loop.time() >= deadline:
        raise TimeoutError("asynchronous output deadline exceeded")


async def _close_iterator(
    iterator: AsyncIterator[bytes], policy: AsyncOutputPolicy, task: asyncio.Task[Any]
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + policy.cleanup_timeout_seconds
    initial = task.cancelling()
    timeout = asyncio.timeout_at(deadline)
    try:
        async with timeout:
            method = getattr(iterator, "aclose", None)
            if method is not None:
                returned = method()
                if not inspect.isawaitable(returned):
                    raise TypeError("iterator.aclose must return an awaitable")
                result = await returned
                _discard_coroutine(result)
                _cancelled(task, initial)
                if result is not None:
                    raise TypeError("iterator.aclose must resolve to None")
            _deadline(loop, deadline)
    except Exception as exc:
        _cancelled(task, initial)
        if timeout.expired() and not isinstance(exc, TimeoutError):
            raise TimeoutError("asynchronous output cleanup deadline exceeded") from exc
        raise
    if timeout.expired():
        raise TimeoutError("asynchronous output cleanup deadline exceeded")
    _cancelled(task, initial)


def _discard_coroutine(value: object) -> None:
    if inspect.iscoroutine(value):
        # Reject a nested coroutine without executing its body or leaking the
        # known unawaited resource. Genuine cleanup control exceptions propagate.
        with suppress(Exception):
            value.close()


def _special_descriptor(value: object, name: str) -> object:
    for base in type.__getattribute__(type(value), "__mro__"):
        namespace = type.__getattribute__(base, "__dict__")
        if name in namespace:
            return namespace[name]
    raise TypeError(f"asynchronous source lacks {name}")


def _iterator(source: AsyncIterable[bytes]) -> AsyncIterator[bytes]:
    # Retain malformed returned coroutines so we can close them. Builtin aiter
    # discards that return value on TypeError. Mirror special-method lookup:
    # class MRO, descriptor binding, never an instance __aiter__ shadow.
    descriptor = _special_descriptor(source, "__aiter__")
    try:
        getter = type.__getattribute__(type(descriptor), "__get__")
    except AttributeError:
        getter = None
    method = descriptor if getter is None else getter(descriptor, source, type(source))
    returned = cast(Callable[[], object], method)()
    _discard_coroutine(returned)
    _special_descriptor(returned, "__anext__")
    return cast(AsyncIterator[bytes], returned)


async def validate_async_output_chunks(
    chunks: AsyncIterable[bytes],
    pipeline: ValidationPipeline,
    *,
    limits: IncrementalLimits | None = None,
    policy: AsyncOutputPolicy | None = None,
    on_progress: Callable[[IncrementalProgress], Awaitable[None]] | None = None,
    close_iterator: bool = False,
) -> IncrementalResult:
    """Pull exactly one chunk at a time, awaiting observation before the next.

    No background task, prefetch queue or model/network adapter is created. The
    observer sees actual provisional values, never semantic approval. Explicitly
    owned iterator cleanup must finish before complete semantic validation runs.
    Source, observer and validator code remain trusted and cooperatively timed.
    """
    if type(close_iterator) is not bool:
        raise ValueError("close_iterator must be boolean")
    options = AsyncOutputPolicy() if policy is None else policy
    if type(options) is not AsyncOutputPolicy:
        raise ValueError("policy must be AsyncOutputPolicy")
    options.__post_init__()
    if on_progress is not None and not callable(on_progress):
        raise ValueError("on_progress must be an asynchronous callable")
    session = IncrementalOutputSession(pipeline, limits)
    task = asyncio.current_task()
    if task is None:
        session.close()
        raise RuntimeError("asynchronous output consumption requires an asyncio task")
    initial = task.cancelling()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + options.timeout_seconds
    iterator: AsyncIterator[bytes] | None = None
    primary: BaseException | None = None
    try:
        timeout = asyncio.timeout_at(deadline)
        try:
            async with timeout:
                iterator = _iterator(chunks)
                while True:
                    _cancelled(task, initial)
                    _deadline(loop, deadline)
                    try:
                        chunk = await anext(iterator)
                    except StopAsyncIteration:
                        break
                    _discard_coroutine(chunk)
                    _cancelled(task, initial)
                    _deadline(loop, deadline)
                    progress = session.feed(chunk)
                    if on_progress is not None:
                        returned = on_progress(progress)
                        if not inspect.isawaitable(returned):
                            raise TypeError("on_progress must return an awaitable")
                        observed = await cast(Awaitable[object], returned)
                        _discard_coroutine(observed)
                        _cancelled(task, initial)
                        if observed is not None:
                            raise TypeError("on_progress must resolve to None")
                    _deadline(loop, deadline)
            if timeout.expired():
                raise TimeoutError("asynchronous output deadline exceeded")
            _cancelled(task, initial)
            _deadline(loop, deadline)
        except BaseException as exc:
            if (
                timeout.expired()
                and isinstance(exc, Exception)
                and not isinstance(exc, TimeoutError)
            ):
                primary = TimeoutError("asynchronous output deadline exceeded")
                primary.__cause__ = exc
            else:
                primary = exc
        try:
            if close_iterator and iterator is not None:
                await _close_iterator(iterator, options, task)
        except Exception:
            if not isinstance(primary, asyncio.CancelledError):
                _cancelled(task, initial)
            if primary is None:
                raise
            primary.add_note("asynchronous output iterator cleanup also failed")
        if isinstance(primary, asyncio.CancelledError):
            raise primary
        _cancelled(task, initial)
        if primary is not None:
            raise primary
        _deadline(loop, deadline)
        # The same parser/pipeline contracts apply; no second JSON parse or
        # per-fragment semantic callbacks are introduced by this async wrapper.
        result = session.finish()
        _cancelled(task, initial)
        _deadline(loop, deadline)
        return result
    finally:
        session.close()


__all__ = ["AsyncOutputPolicy", "validate_async_output_chunks"]
