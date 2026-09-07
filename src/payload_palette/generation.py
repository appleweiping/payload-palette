"""Provider-neutral, budgeted asynchronous generation and validation lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol, cast

from payload_palette.errors import PayloadValidationError, ValidationIssue
from payload_palette.output_schema import (
    JSONValue,
    OutputContractError,
    OutputSchema,
    _schema_path_possible,
    _text,
)
from payload_palette.output_validation import ValidationPipeline

AttemptStatus = Literal[
    "accepted",
    "invalid_output",
    "provider_error",
    "provider_contract",
    "timeout",
    "token_budget",
    "response_budget",
    "validator_budget",
    "validator_error",
]
Termination = Literal[
    "accepted",
    "attempts_exhausted",
    "provider_error",
    "provider_contract",
    "timeout",
    "deadline",
    "token_budget",
    "response_budget",
    "validator_budget",
    "validator_error",
]


def _bounded_integer(name: str, value: object, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-reported usage, never estimated from response characters."""

    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        _bounded_integer("input_tokens", self.input_tokens, 0, 1_000_000_000)
        _bounded_integer("output_tokens", self.output_tokens, 0, 1_000_000_000)

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class GeneratedResponse:
    """One complete JSON response and explicit usage from an asynchronous adapter."""

    text: str = field(repr=False)
    usage: TokenUsage

    def __post_init__(self) -> None:
        if type(self.text) is not str or len(self.text) > 8_000_000 or not _text(self.text):
            raise ValueError("response text must be a bounded Unicode scalar string")
        if type(self.usage) is not TokenUsage:
            raise ValueError("response usage must be TokenUsage")
        self.usage.__post_init__()


@dataclass(frozen=True, slots=True)
class GenerationFeedback:
    """A stable failure code and declared path; raw values/messages are excluded."""

    code: str
    path: str = "$"

    def __post_init__(self) -> None:
        if type(self.code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.code) is None:
            raise ValueError("feedback code must be a lowercase identifier")
        if (
            type(self.path) is not str
            or not self.path.startswith("$")
            or len(self.path) > 2_048
            or not _text(self.path)
        ):
            raise ValueError("feedback path must be a bounded path string")

    def to_dict(self) -> dict[str, JSONValue]:
        return {"code": self.code, "path": self.path}


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """An isolated provider request. Schema access always returns a fresh JSON copy.

    The instruction goes to the explicitly supplied provider. The runner never
    sends earlier raw responses in a re-ask; only bounded feedback is included.
    """

    instruction: str = field(repr=False)
    attempt: int
    max_output_tokens: int
    remaining_total_tokens: int
    max_response_bytes: int
    feedback: tuple[GenerationFeedback, ...]
    _schema_json: str = field(repr=False)

    @property
    def schema(self) -> dict[str, JSONValue]:
        return cast(dict[str, JSONValue], json.loads(self._schema_json))


class AsyncModelProvider(Protocol):
    """An actual asynchronous adapter. Implementations must cooperate with cancellation."""

    async def generate(self, request: GenerationRequest) -> GeneratedResponse:
        """Return complete output and usage without swallowing task cancellation."""
        ...


@dataclass(frozen=True, slots=True)
class GenerationPolicy:
    """Hard ceilings on attempt count, response allocation and reported usage.

    Provider input-token cost is known only after a response. The output-token
    hint and usage checks cannot guarantee a remote provider's billing ceiling.
    Timeouts are cooperative; Python cannot forcibly stop a misbehaving adapter.
    """

    max_attempts: int = 3
    max_output_tokens: int = 512
    max_total_tokens: int = 4_096
    max_response_bytes: int = 1_000_000
    max_total_response_bytes: int = 4_000_000
    max_validator_invocations: int = 1_000
    max_feedback: int = 16
    max_instruction_characters: int = 100_000
    attempt_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_attempts", 32),
            ("max_output_tokens", 128_000),
            ("max_total_tokens", 1_000_000),
            ("max_response_bytes", 8_000_000),
            ("max_total_response_bytes", 64_000_000),
            ("max_validator_invocations", 100_000),
            ("max_feedback", 100),
            ("max_instruction_characters", 1_000_000),
        ):
            _bounded_integer(name, getattr(self, name), 1, maximum)
        for name in ("attempt_timeout_seconds", "total_timeout_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= 86_400:
                raise ValueError(f"{name} must be finite and between zero and 86400 seconds")


@dataclass(frozen=True, slots=True)
class GenerationAttempt:
    number: int
    status: AttemptStatus
    usage: TokenUsage | None
    response_bytes: int
    validator_invocations: int
    feedback: tuple[GenerationFeedback, ...]

    def to_dict(self) -> dict[str, JSONValue]:
        return {
            "number": self.number,
            "status": self.status,
            "usage": None
            if self.usage is None
            else {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
            },
            "response_bytes": self.response_bytes,
            "validator_invocations": self.validator_invocations,
            "feedback": [item.to_dict() for item in self.feedback],
        }


@dataclass(frozen=True, slots=True)
class GenerationReport:
    """Privacy-minimized attempt history; accepted output is explicitly opt-in."""

    termination: Termination
    attempts: tuple[GenerationAttempt, ...]
    reported_tokens: int
    response_bytes: int
    validator_invocations: int
    usage_complete: bool
    _output_json: str | None = field(repr=False)

    @property
    def valid(self) -> bool:
        return self.termination == "accepted"

    @property
    def output(self) -> JSONValue:
        return None if self._output_json is None else cast(JSONValue, json.loads(self._output_json))

    def to_dict(self, *, include_output: bool = False) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "valid": self.valid,
            "termination": self.termination,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "reported_tokens": self.reported_tokens,
            "response_bytes": self.response_bytes,
            "validator_invocations": self.validator_invocations,
            "usage_complete": self.usage_complete,
        }
        if include_output:
            result["output"] = self.output
        return result


def _declared_path(path: str, schema: OutputSchema) -> str:
    """Prevent undeclared generated object keys from entering re-ask/history data."""

    if not path.startswith("$") or len(path) > 2_048:
        return "$"
    position = 1
    segments: list[str | int] = []
    decoder = json.JSONDecoder()
    while position < len(path):
        if path[position] != "[":
            return "$"
        try:
            segment, end = decoder.raw_decode(path, position + 1)
        except ValueError:
            return "$"
        if end >= len(path) or path[end] != "]":
            return "$"
        if type(segment) not in (str, int):
            return "$"
        segments.append(segment)
        position = end + 1
    return path if _schema_path_possible(schema, tuple(segments), allow_dynamic=False) else "$"


def _feedback(
    issues: tuple[ValidationIssue, ...], schema: OutputSchema, maximum: int
) -> tuple[GenerationFeedback, ...]:
    result: list[GenerationFeedback] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        path = _declared_path(issue.path, schema)
        identity = (issue.code, path)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(GenerationFeedback(*identity))
        if len(result) >= maximum:
            break
    return tuple(result)


def _utf8_size(text: str) -> int:
    """Measure validated Unicode scalars before allocating a response-byte copy."""

    size = 0
    for character in text:
        point = ord(character)
        if point < 0x80:
            size += 1
        elif point < 0x800:
            size += 2
        elif point < 0x10000:
            size += 3
        else:
            size += 4
    return size


def _propagate_cancellation() -> None:
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError from None


@dataclass(frozen=True, slots=True)
class AsyncGenerationRunner:
    """Bounded sequential generation/re-ask with final semantic validation.

    Only invalid generated output triggers another attempt. Provider failures,
    timeouts, and exhausted resource budgets terminate the run. External task
    cancellation propagates as CancelledError and is never converted to a report.
    """

    pipeline: ValidationPipeline
    provider: AsyncModelProvider
    policy: GenerationPolicy = field(default_factory=GenerationPolicy)

    def __post_init__(self) -> None:
        if (
            type(self.pipeline) is not ValidationPipeline
            or type(self.policy) is not GenerationPolicy
        ):
            raise ValueError("pipeline and policy must be their declared immutable types")
        if not inspect.iscoroutinefunction(getattr(self.provider, "generate", None)):
            raise ValueError("provider.generate must be an async function")

    async def run(self, instruction: str) -> GenerationReport:
        if (
            type(instruction) is not str
            or len(instruction) > self.policy.max_instruction_characters
            or not _text(instruction)
        ):
            raise ValueError("instruction must be a bounded Unicode scalar string")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.policy.total_timeout_seconds
        schema_json = json.dumps(
            self.pipeline.schema.json_schema(), ensure_ascii=True, allow_nan=False
        )
        attempts: list[GenerationAttempt] = []
        reported_tokens = 0
        response_bytes = 0
        invocations = 0
        usage_complete = True
        feedback: tuple[GenerationFeedback, ...] = ()

        def report(termination: Termination, output: JSONValue = None) -> GenerationReport:
            return GenerationReport(
                termination,
                tuple(attempts),
                reported_tokens,
                response_bytes,
                invocations,
                usage_complete,
                json.dumps(output, ensure_ascii=True, allow_nan=False)
                if termination == "accepted"
                else None,
            )

        def append(
            status: AttemptStatus,
            usage: TokenUsage | None = None,
            size: int = 0,
            calls: int = 0,
            problems: tuple[GenerationFeedback, ...] = (),
        ) -> None:
            attempts.append(
                GenerationAttempt(len(attempts) + 1, status, usage, size, calls, problems)
            )

        for number in range(1, self.policy.max_attempts + 1):
            # Cancellation is observed even when preceding local validation had no await.
            await asyncio.sleep(0)
            remaining_time = deadline - loop.time()
            if remaining_time <= 0:
                return report("deadline")
            remaining_tokens = self.policy.max_total_tokens - reported_tokens
            if remaining_tokens <= 0:
                return report("token_budget")
            if response_bytes >= self.policy.max_total_response_bytes:
                return report("response_budget")
            remaining_calls = self.policy.max_validator_invocations - invocations
            if remaining_calls <= 0 and self.pipeline.rules:
                return report("validator_budget")
            request = GenerationRequest(
                instruction,
                number,
                min(self.policy.max_output_tokens, remaining_tokens),
                remaining_tokens,
                min(
                    self.policy.max_response_bytes,
                    self.policy.max_total_response_bytes - response_bytes,
                ),
                feedback,
                schema_json,
            )
            attempt_deadline = min(loop.time() + self.policy.attempt_timeout_seconds, deadline)
            deadline_limited = attempt_deadline == deadline
            # Asyncio can deliver timers one clock-resolution window early.
            # Preserve which budget armed the timer rather than inferring its
            # cause solely from a later (possibly coarse) clock observation.
            timeout = asyncio.timeout_at(attempt_deadline)
            try:
                async with timeout:
                    response = await self.provider.generate(request)
            except TimeoutError:
                _propagate_cancellation()
                usage_complete = False
                if timeout.expired():
                    append("timeout")
                    return report(
                        "deadline" if deadline_limited or loop.time() >= deadline else "timeout"
                    )
                append("provider_error")
                return report("provider_error")
            except Exception:
                _propagate_cancellation()
                usage_complete = False
                if timeout.expired():
                    append("timeout")
                    return report(
                        "deadline" if deadline_limited or loop.time() >= deadline else "timeout"
                    )
                append("provider_error")
                return report("provider_error")
            _propagate_cancellation()
            # Suppressing cancellation or blocking the loop must not permit late acceptance.
            if timeout.expired() or loop.time() >= attempt_deadline:
                usage_complete = False
                append("timeout")
                return report(
                    "deadline" if deadline_limited or loop.time() >= deadline else "timeout"
                )
            try:
                if inspect.iscoroutine(response):
                    response.close()
                if type(response) is not GeneratedResponse:
                    raise ValueError("invalid provider response")
                response.__post_init__()
            except Exception:
                _propagate_cancellation()
                usage_complete = False
                append("provider_contract")
                return report("provider_contract")
            reported_tokens += response.usage.total
            if (
                reported_tokens > self.policy.max_total_tokens
                or response.usage.output_tokens > request.max_output_tokens
            ):
                append("token_budget", response.usage)
                return report("token_budget")
            if len(response.text) > self.policy.max_response_bytes:
                append("response_budget", response.usage)
                return report("response_budget")
            measured_bytes = _utf8_size(response.text)
            response_bytes += measured_bytes
            if (
                measured_bytes > self.policy.max_response_bytes
                or response_bytes > self.policy.max_total_response_bytes
            ):
                append("response_budget", response.usage, measured_bytes)
                return report("response_budget")
            payload = response.text.encode("utf-8")
            active_pipeline = replace(
                self.pipeline,
                limits=replace(
                    self.pipeline.limits,
                    max_invocations=max(
                        1, min(self.pipeline.limits.max_invocations, remaining_calls)
                    ),
                ),
            )
            try:
                validated = active_pipeline.validate_json_bytes(
                    payload, max_input_bytes=self.policy.max_response_bytes
                )
            except PayloadValidationError as exc:
                feedback = _feedback(exc.issues, self.pipeline.schema, self.policy.max_feedback)
                append("invalid_output", response.usage, len(payload), problems=feedback)
            except OutputContractError:
                feedback = (GenerationFeedback("output_contract"),)
                append("invalid_output", response.usage, len(payload), problems=feedback)
            else:
                invocations += validated.invocations
                feedback = _feedback(
                    validated.issues, self.pipeline.schema, self.policy.max_feedback
                )
                if any(
                    outcome.status == "error" and outcome.code == "validator_budget"
                    for outcome in validated.outcomes
                ):
                    append(
                        "validator_budget",
                        response.usage,
                        len(payload),
                        validated.invocations,
                        feedback,
                    )
                    return report("validator_budget")
                if any(outcome.status == "error" for outcome in validated.outcomes):
                    append(
                        "validator_error",
                        response.usage,
                        len(payload),
                        validated.invocations,
                        feedback,
                    )
                    return report("validator_error")
                await asyncio.sleep(0)
                if loop.time() >= deadline:
                    append("timeout", response.usage, len(payload), validated.invocations)
                    return report("deadline")
                if validated.valid:
                    append("accepted", response.usage, len(payload), validated.invocations)
                    return report("accepted", validated.output)
                append(
                    "invalid_output", response.usage, len(payload), validated.invocations, feedback
                )
        await asyncio.sleep(0)
        return report("deadline" if loop.time() >= deadline else "attempts_exhausted")
