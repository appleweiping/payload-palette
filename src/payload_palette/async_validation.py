"""Bounded asynchronous read-only semantic checks after complete synchronous validation."""

from __future__ import annotations

import asyncio
import inspect
import re
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from .errors import ValidationIssue
from .ingress import decode_json_bytes
from .output_schema import (
    JSONValue,
    OutputPath,
    _schema_path_possible,
    _text,
    output_path,
    snapshot_json,
)
from .output_validation import (
    _NO_FIX,
    OutcomeStatus,
    OutputReport,
    RuleContext,
    RuleOutcome,
    RuleResult,
    ValidationPipeline,
    _get,
)


class AsyncOutputValidator(Protocol):
    """Trusted asynchronous check; the runtime owns the fresh returned coroutine."""

    async def check(self, value: JSONValue, context: RuleContext) -> RuleResult: ...


@dataclass(frozen=True, slots=True)
class AsyncRuleBinding:
    """Attach a reject-only asynchronous rule to an exact final-output path."""

    rule_id: str
    path: OutputPath
    validator: AsyncOutputValidator

    def __post_init__(self) -> None:
        if (
            type(self.rule_id) is not str
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.rule_id) is None
        ):
            raise ValueError("rule_id must be a lowercase identifier of at most 64 characters")
        if (
            type(self.path) is not tuple
            or len(self.path) > 32
            or any(
                (type(part) is not int or not 0 <= part < 100_000)
                and (not _text(part) or len(cast(str, part)) > 256)
                for part in self.path
            )
        ):
            raise ValueError("path must contain at most 32 bounded object keys or array indices")
        if not inspect.iscoroutinefunction(getattr(self.validator, "check", None)):
            raise ValueError("validator.check must be an async function")


@dataclass(frozen=True, slots=True)
class AsyncValidationPolicy:
    """Cooperative deadlines, not a hard preemption or cleanup-time guarantee."""

    max_concurrency: int = 4
    per_validator_timeout_seconds: float = 10.0
    total_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 32:
            raise ValueError("max_concurrency must be an integer between 1 and 32")
        for name in ("per_validator_timeout_seconds", "total_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= 86_400:
                raise ValueError(f"{name} must be finite and between zero and 86400 seconds")


def _discard_coroutine(value: object) -> None:
    if inspect.iscoroutine(value):
        # Ordinary cleanup prose is not part of the diagnostic contract.
        with suppress(Exception):
            value.close()


def _error(code: str) -> RuleResult:
    messages = {
        "validator_contract": "asynchronous validator violated its callback/result contract",
        "validator_timeout": "asynchronous validator deadline exceeded",
        "validation_deadline": "asynchronous validation total deadline exceeded",
        "validator_budget": "validator invocation limit exceeded",
    }
    return RuleResult(False, code, messages[code])


@dataclass(slots=True)
class _Completion:
    result: RuleResult | None = None
    control: BaseException | None = None
    error: bool = False


@dataclass(slots=True)
class _Job:
    index: int
    binding: AsyncRuleBinding
    entered: bool = False
    timer: asyncio.Timeout | None = None


def _pending_cancel(task: asyncio.Task[Any], baseline: int) -> None:
    if task.cancelling() > baseline:
        raise asyncio.CancelledError


def _primary(first: BaseException | None, later: BaseException | None) -> BaseException | None:
    if first is None or (
        isinstance(first, Exception) and later is not None and not isinstance(later, Exception)
    ):
        return later
    return first


async def _settle(tasks: dict[asyncio.Task[_Completion], _Job]) -> BaseException | None:
    """Cancel once, then drain owned children despite repeated caller cancellation."""
    for task, job in tasks.items():
        if not task.done():
            # A later per-check timer must not cancel an already running finally.
            if job.timer is not None and not job.timer.expired():
                job.timer.reschedule(None)
            if not task.cancelling():
                task.cancel()
    if not tasks:
        return None
    drained = asyncio.gather(*tasks, return_exceptions=True)
    primary: BaseException | None = None
    while not drained.done():
        try:
            await asyncio.shield(drained)
        except BaseException as exc:
            primary = _primary(primary, exc)
    # Retrieve all results/exceptions, including children canceled before entry.
    for result in drained.result():
        if isinstance(result, _Completion) and not isinstance(
            result.control, asyncio.CancelledError
        ):
            primary = _primary(primary, result.control)
    return primary


@dataclass(frozen=True, slots=True)
class AsyncValidationPipeline:
    """Run sync repairs first, then bounded concurrent reject-only final checks.

    Async callbacks never mutate the accepted candidate: each receives an
    isolated copy. All created tasks settle before return or interruption.
    Trusted callbacks that block the loop or resist cancellation can delay this
    settlement indefinitely; no background-task timeout escape is provided.
    """

    pipeline: ValidationPipeline
    rules: tuple[AsyncRuleBinding, ...] = ()
    policy: AsyncValidationPolicy = field(default_factory=AsyncValidationPolicy)

    def __post_init__(self) -> None:
        if (
            type(self.pipeline) is not ValidationPipeline
            or type(self.policy) is not AsyncValidationPolicy
        ):
            raise ValueError("pipeline and policy must be their declared immutable types")
        if type(self.rules) is not tuple or len(self.rules) + len(self.pipeline.rules) > 256:
            raise ValueError("combined synchronous/asynchronous bindings must be at most 256")
        ids = {binding.rule_id for binding in self.pipeline.rules}
        for binding in self.rules:
            if type(binding) is not AsyncRuleBinding:
                raise ValueError("rules must contain AsyncRuleBinding objects")
            binding.__post_init__()
            if binding.rule_id in ids:
                raise ValueError("rule ids must be unique across both validation stages")
            ids.add(binding.rule_id)
            if not _schema_path_possible(self.pipeline.schema, binding.path):
                raise ValueError(
                    f"rule {binding.rule_id!r} has a path incompatible with the schema"
                )

    async def _execute(self, job: _Job, value: JSONValue, deadline: float) -> _Completion:
        loop = asyncio.get_running_loop()
        task = cast(asyncio.Task[Any], asyncio.current_task())
        baseline = task.cancelling()
        end = min(deadline, loop.time() + self.policy.per_validator_timeout_seconds)
        code = "validation_deadline" if end == deadline else "validator_timeout"
        # The parent alone owns total-deadline cancellation. Competing timers
        # could otherwise cancel this same callback twice during cleanup.
        timer = asyncio.timeout_at(end if code == "validator_timeout" else None)
        job.timer = timer
        try:
            async with timer:
                copied = snapshot_json(value, self.pipeline.limits)
                if loop.time() >= end:
                    return _Completion(_error(code), error=True)
                method = job.binding.validator.check
                if not callable(method):
                    raise TypeError("check must remain callable")
                job.entered = True
                returned = method(
                    copied, RuleContext(job.binding.rule_id, job.binding.path, "final")
                )
                # Tasks/Futures may belong to callers. Never await or cancel
                # those borrowed resources; only a fresh native coroutine transfers.
                if not inspect.iscoroutine(returned):
                    raise TypeError("check must return a fresh native coroutine")
                if inspect.getcoroutinestate(returned) != inspect.CORO_CREATED:
                    raise TypeError("check returned an already-started coroutine")
                result = await returned
                # Swallowed cancellation can itself return a malformed resource.
                # Release directly visible native coroutines before propagating
                # cancellation, while leaving borrowed Tasks/Futures untouched.
                _discard_coroutine(result.fix if type(result) is RuleResult else result)
                _pending_cancel(task, baseline)
                if type(result) is not RuleResult:
                    raise TypeError("check must resolve to RuleResult")
                result.__post_init__()
                if result.fix is not _NO_FIX:
                    raise TypeError("asynchronous repair proposals are unsupported")
            if timer.expired() or loop.time() >= end:
                return _Completion(_error(code), error=True)
            return _Completion(result)
        except BaseException as exc:
            if isinstance(exc, (Exception, asyncio.CancelledError)):
                if timer.expired():
                    return _Completion(_error(code), error=True)
                if task.cancelling() > baseline or isinstance(exc, asyncio.CancelledError):
                    return _Completion(control=asyncio.CancelledError())
                return _Completion(_error("validator_contract"), error=True)
            # Keep genuine control exceptions out of Task's event-loop machinery
            # until the parent has settled all siblings.
            return _Completion(control=exc)
        finally:
            job.timer = None

    async def validate_json_bytes(
        self, payload: bytes | bytearray | memoryview, *, max_input_bytes: int = 4_000_000
    ) -> OutputReport:
        """Strict whole JSON decoding is included in the total deadline."""
        return await self._validate(payload, encoded=True, max_input_bytes=max_input_bytes)

    async def validate(self, value: object) -> OutputReport:
        return await self._validate(value)

    async def _validate(
        self, value: object, *, encoded: bool = False, max_input_bytes: int = 4_000_000
    ) -> OutputReport:
        loop = asyncio.get_running_loop()
        parent = cast(asyncio.Task[Any], asyncio.current_task())
        baseline = parent.cancelling()
        deadline = loop.time() + self.policy.total_timeout_seconds
        # Deliver already pending cancellation before entering synchronous code.
        await asyncio.sleep(0)
        if encoded:
            value = decode_json_bytes(
                cast(bytes | bytearray | memoryview, value), max_input_bytes=max_input_bytes
            )
        base = self.pipeline.validate(value)
        _pending_cancel(parent, baseline)
        jobs: dict[asyncio.Task[_Completion], _Job] = {}
        active: set[asyncio.Task[_Completion]] = set()
        completed: dict[int, _Completion] = {}
        budget = self.pipeline.limits.max_invocations - base.invocations
        position = 0
        failure: str | None = None
        if loop.time() >= deadline:
            failure = "validation_deadline"
        elif not base.valid:
            return base
        document = base.output if failure is None else None
        primary: BaseException | None = None
        try:
            while failure in (None, "validator_budget"):
                _pending_cancel(parent, baseline)
                if loop.time() >= deadline:
                    failure = "validation_deadline"
                    break
                while (
                    failure is None
                    and position < len(self.rules)
                    and len(active) < self.policy.max_concurrency
                ):
                    binding = self.rules[position]
                    index = position
                    position += 1
                    present, current = _get(document, binding.path)
                    if not present:
                        continue
                    if len(jobs) >= budget:
                        failure = "validator_budget"
                        break
                    job = _Job(index, binding)
                    coroutine = self._execute(job, current, deadline)
                    try:
                        task = asyncio.create_task(coroutine)
                    except BaseException:
                        coroutine.close()
                        raise
                    jobs[task] = job
                    active.add(task)
                if not active:
                    break
                done, _ = await asyncio.wait(
                    active,
                    timeout=max(0.0, deadline - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                _pending_cancel(parent, baseline)
                if not done:
                    failure = "validation_deadline"
                    break
                for task in sorted(done, key=lambda item: jobs[item].index):
                    active.remove(task)
                    result = task.result()
                    completed[jobs[task].index] = result
                    if result.control is not None:
                        raise result.control
                    if (
                        result.error
                        and result.result is not None
                        and result.result.code == "validation_deadline"
                    ):
                        failure = "validation_deadline"
        except BaseException as exc:
            primary = exc
        finally:
            cleanup_control = await _settle(jobs)
            primary = _primary(primary, cleanup_control)
        if primary is not None:
            raise primary
        _pending_cancel(parent, baseline)
        if loop.time() >= deadline:
            failure = "validation_deadline"
        # Collect late completions after owned cancellation without accepting
        # partially checked output. Tasks canceled before entry consumed a
        # reservation, not a reported callback invocation.
        for task, job in jobs.items():
            if job.index not in completed and not task.cancelled():
                result = task.result()
                if result.control is not None:
                    if failure is None or not isinstance(result.control, asyncio.CancelledError):
                        raise result.control
                else:
                    completed[job.index] = result
        issues = list(base.issues)
        outcomes = list(base.outcomes)
        rejected = not base.valid or failure is not None
        for index in sorted(completed):
            completion = completed[index]
            decision = completion.result
            if decision is None:
                raise RuntimeError(
                    "completed validation has neither a result nor a propagated control"
                )
            binding = self.rules[index]
            path = output_path(binding.path)
            status: OutcomeStatus = (
                "error" if completion.error else "passed" if decision.valid else "rejected"
            )
            outcomes.append(
                RuleOutcome(binding.rule_id, path, "final", status, decision.code, decision.message)
            )
            if not decision.valid:
                rejected = True
                if len(issues) < self.pipeline.limits.max_issues:
                    issues.append(ValidationIssue(decision.code, decision.message, path))
        _pending_cancel(parent, baseline)
        if loop.time() >= deadline:
            failure = "validation_deadline"
            rejected = True
        if failure is not None and len(issues) < self.pipeline.limits.max_issues:
            decision = _error(failure)
            issues.append(ValidationIssue(decision.code, decision.message))
        return OutputReport(
            not rejected,
            tuple(issues),
            tuple(outcomes),
            base.invocations + sum(job.entered for job in jobs.values()),
            None if rejected else base._output_json,
        )
