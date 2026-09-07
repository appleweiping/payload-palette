"""Deterministic synchronous semantic validation of structured model output."""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol, cast

from payload_palette.errors import ValidationIssue
from payload_palette.ingress import decode_json_bytes
from payload_palette.output_schema import (
    JSONValue,
    OutputContractError,
    OutputLimits,
    OutputPath,
    OutputSchema,
    _schema_path_possible,
    _text,
    output_path,
    snapshot_json,
)

_NO_FIX = object()
FailureAction = Literal["reject", "fix", "filter"]
OutcomeStatus = Literal["passed", "rejected", "fixed", "filtered", "error"]


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Identity and exact current JSON path, without ambient metadata or secrets."""

    rule_id: str
    path: OutputPath
    phase: Literal["initial", "repair", "final"]


@dataclass(frozen=True, slots=True)
class RuleResult:
    """A callback decision; fixes are suggestions until revalidation succeeds."""

    valid: bool
    code: str = ""
    message: str = ""
    fix: object = _NO_FIX

    def __post_init__(self) -> None:
        if type(self.valid) is not bool:
            raise ValueError("valid must be a boolean")
        if not _text(self.message) or len(self.message) > 2_048:
            raise ValueError("message must contain at most 2048 Unicode scalar characters")
        if type(self.code) is not str or (
            not self.valid and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.code) is None
        ):
            raise ValueError("failure code must be a lowercase identifier of at most 64 characters")
        if self.valid and (self.code or self.message or self.fix is not _NO_FIX):
            raise ValueError("successful results cannot include failures or fixes")


class OutputValidator(Protocol):
    """Trusted synchronous callback. The supplied value is an isolated JSON copy."""

    def check(self, value: JSONValue, context: RuleContext) -> RuleResult:
        """Return a structured success or failure; never mutate ambient state."""
        ...


@dataclass(frozen=True, slots=True)
class RuleBinding:
    """Attach a validator to one explicit field path; absent optional paths skip.

    Filtering supports object members only. Removing a required member causes
    final schema rejection; array element removal is deliberately not implicit.
    """

    rule_id: str
    path: OutputPath
    validator: OutputValidator
    on_fail: FailureAction = "reject"

    def __post_init__(self) -> None:
        if (
            type(self.rule_id) is not str
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.rule_id) is None
        ):
            raise ValueError("rule_id must be a lowercase identifier of at most 64 characters")
        if type(self.path) is not tuple or len(self.path) > 32:
            raise ValueError("path must be a tuple with at most 32 segments")
        if any(
            (type(part) is not int or not 0 <= part < 100_000)
            and (not _text(part) or len(cast(str, part)) > 256)
            for part in self.path
        ):
            raise ValueError("path segments must be bounded Unicode keys or nonnegative indices")
        if self.on_fail not in ("reject", "fix", "filter"):
            raise ValueError("on_fail must be reject, fix, or filter")
        if self.on_fail == "filter" and (not self.path or type(self.path[-1]) is not str):
            raise ValueError("filter requires an object-member path")
        method = getattr(self.validator, "check", None)
        if not callable(method) or inspect.iscoroutinefunction(method):
            raise ValueError("validator.check must be a synchronous callable")


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    rule_id: str
    path: str
    phase: str
    status: OutcomeStatus
    code: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "path": self.path,
            "phase": self.phase,
            "status": self.status,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class OutputReport:
    """Immutable run record; output returns a fresh copy only for accepted runs.

    Validator messages are caller supplied and can contain sensitive text. The
    default report serializer omits output but is not a message redaction system.
    """

    valid: bool
    issues: tuple[ValidationIssue, ...]
    outcomes: tuple[RuleOutcome, ...]
    invocations: int
    _output_json: str | None

    @property
    def output(self) -> JSONValue:
        return None if self._output_json is None else cast(JSONValue, json.loads(self._output_json))

    def to_dict(self, *, include_output: bool = False) -> dict[str, JSONValue]:
        result: dict[str, JSONValue] = {
            "valid": self.valid,
            "issues": [cast(JSONValue, issue.to_dict()) for issue in self.issues],
            "outcomes": [cast(JSONValue, outcome.to_dict()) for outcome in self.outcomes],
            "invocations": self.invocations,
        }
        if include_output:
            result["output"] = self.output
        return result


def _get(document: JSONValue, path: OutputPath) -> tuple[bool, JSONValue]:
    current = document
    for part in path:
        # Separate branches preserve the key/index type relationship for static checking.
        if type(part) is str and isinstance(current, dict) and part in current:  # noqa: SIM114
            current = current[part]
        elif type(part) is int and isinstance(current, list) and part < len(current):
            current = current[part]
        else:
            return False, None
    return True, current


def _set(document: JSONValue, path: OutputPath, value: JSONValue) -> JSONValue:
    if not path:
        return value
    _, parent = _get(document, path[:-1])
    segment = path[-1]
    if isinstance(parent, dict) and isinstance(segment, str):  # noqa: SIM114
        parent[segment] = value
    elif isinstance(parent, list) and isinstance(segment, int):
        parent[segment] = value
    else:  # Internal invariant: the caller resolved this path immediately before mutation.
        raise OutputContractError("resolved path no longer exists")
    return document


class _CallbackFailure(Exception):
    pass


def _check_binding_path(schema: OutputSchema, binding: RuleBinding) -> None:
    """Reject paths that cannot exist under the declared structural contract."""

    if not _schema_path_possible(schema, binding.path):
        raise ValueError(f"rule {binding.rule_id!r} has a path incompatible with the schema")


@dataclass(frozen=True, slots=True)
class ValidationPipeline:
    """Validate structure, execute ordered rules, and recheck the final output.

    Fixes get one attempt and one immediate callback recheck. If any fix/filter
    changes the document, every remaining rule is verified once on the final
    document without further repairs. This catches later fixes invalidating an
    earlier rule. Failure never exposes a partially repaired output as accepted.
    """

    schema: OutputSchema
    rules: tuple[RuleBinding, ...] = ()
    limits: OutputLimits = field(default_factory=OutputLimits)

    def __post_init__(self) -> None:
        if type(self.schema) is not OutputSchema:
            raise ValueError("schema must be an OutputSchema")
        if (
            type(self.rules) is not tuple
            or len(self.rules) > 256
            or any(type(rule) is not RuleBinding for rule in self.rules)
        ):
            raise ValueError("rules must be a tuple containing at most 256 RuleBinding objects")
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("rule ids must be unique within a pipeline")
        if type(self.limits) is not OutputLimits:
            raise ValueError("limits must be OutputLimits")
        for binding in self.rules:
            _check_binding_path(self.schema, binding)

    def validate_json_bytes(
        self, payload: bytes | bytearray | memoryview, *, max_input_bytes: int = 4_000_000
    ) -> OutputReport:
        """Use the existing strict byte decoder before any semantic callback."""

        return self.validate(decode_json_bytes(payload, max_input_bytes=max_input_bytes))

    def validate(self, value: object) -> OutputReport:
        document = snapshot_json(value, self.limits)
        issues = list(self.schema.validate(document, limits=self.limits))
        outcomes: list[RuleOutcome] = []
        invocations = 0
        changed = False
        exhausted = False

        def record(
            binding: RuleBinding, phase: str, status: OutcomeStatus, result: RuleResult
        ) -> None:
            outcomes.append(
                RuleOutcome(
                    binding.rule_id,
                    output_path(binding.path),
                    phase,
                    status,
                    result.code,
                    result.message,
                )
            )
            if status in ("rejected", "error") and len(issues) < self.limits.max_issues:
                issues.append(
                    ValidationIssue(result.code, result.message, output_path(binding.path))
                )

        def call(binding: RuleBinding, current: JSONValue, phase: str) -> RuleResult:
            nonlocal invocations, exhausted
            if invocations >= self.limits.max_invocations:
                exhausted = True
                result = RuleResult(
                    False, "validator_budget", "validator invocation limit exceeded"
                )
                record(binding, phase, "error", result)
                raise _CallbackFailure
            invocations += 1
            try:
                candidate = binding.validator.check(
                    snapshot_json(current, self.limits),
                    RuleContext(
                        binding.rule_id,
                        binding.path,
                        cast(Literal["initial", "repair", "final"], phase),
                    ),
                )
                if inspect.iscoroutine(candidate):
                    candidate.close()
                if type(candidate) is not RuleResult:
                    raise ValueError("invalid validator result")
                candidate.__post_init__()
                return candidate
            except Exception as exc:
                result = RuleResult(
                    False, "validator_contract", "validator raised or returned an invalid result"
                )
                record(binding, phase, "error", result)
                raise _CallbackFailure from exc

        if not issues:
            for binding in self.rules:
                if exhausted:
                    break
                present, current = _get(document, binding.path)
                if not present:
                    continue
                try:
                    result = call(binding, current, "initial")
                    if result.valid:
                        record(binding, "initial", "passed", result)
                    elif binding.on_fail == "reject":
                        record(binding, "initial", "rejected", result)
                    elif binding.on_fail == "filter":
                        _, parent = _get(document, binding.path[:-1])
                        del cast(dict[str, JSONValue], parent)[cast(str, binding.path[-1])]
                        changed = True
                        record(binding, "initial", "filtered", result)
                    elif result.fix is _NO_FIX:
                        record(binding, "initial", "rejected", result)
                    else:
                        try:
                            replacement = snapshot_json(result.fix, self.limits)
                        except OutputContractError:
                            record(
                                binding,
                                "repair",
                                "error",
                                RuleResult(
                                    False, "invalid_fix", "fix violates JSON resource/type contract"
                                ),
                            )
                            continue
                        checked = call(binding, replacement, "repair")
                        if not checked.valid:
                            record(binding, "repair", "rejected", checked)
                            continue
                        document = _set(document, binding.path, replacement)
                        try:
                            snapshot_json(document, self.limits)
                        except OutputContractError:
                            record(
                                binding,
                                "repair",
                                "error",
                                RuleResult(
                                    False,
                                    "output_budget",
                                    "repaired output exceeds resource limits",
                                ),
                            )
                            exhausted = True
                            break
                        changed = True
                        record(binding, "repair", "fixed", result)
                except _CallbackFailure:
                    continue
            if changed:
                # Recheck whole-document bounds, since individually bounded fixes can accumulate.
                try:
                    final_issues = self.schema.validate(document, limits=self.limits)
                except OutputContractError:
                    final_issues = (
                        ValidationIssue(
                            "output_budget", "repaired output exceeds resource limits", "$"
                        ),
                    )
                issues.extend(final_issues[: max(0, self.limits.max_issues - len(issues))])
                if not issues and not exhausted:
                    for binding in self.rules:
                        if exhausted:
                            break
                        present, current = _get(document, binding.path)
                        if not present:
                            continue
                        try:
                            result = call(binding, current, "final")
                            record(
                                binding, "final", "passed" if result.valid else "rejected", result
                            )
                        except _CallbackFailure:
                            continue
        valid = not issues
        serialized = (
            json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            if valid
            else None
        )
        return OutputReport(valid, tuple(issues), tuple(outcomes), invocations, serialized)


@dataclass(frozen=True, slots=True)
class TrimmedString:
    """Require edge-whitespace-free strings and suggest an exact strip fix."""

    def check(self, value: JSONValue, context: RuleContext) -> RuleResult:
        if not isinstance(value, str):
            return RuleResult(False, "string_required", "expected a string")
        trimmed = value.strip()
        if trimmed != value:
            return RuleResult(
                False, "edge_whitespace", "string has leading/trailing whitespace", trimmed
            )
        return RuleResult(True)


@dataclass(frozen=True, slots=True)
class StringChoices:
    """Enforce exact string membership; optionally suggest unambiguous case fixes."""

    choices: tuple[str, ...]
    fix_case: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.choices) is not tuple
            or not 1 <= len(self.choices) <= 256
            or any(not _text(item) or len(item) > 2_048 for item in self.choices)
            or len(set(self.choices)) != len(self.choices)
            or type(self.fix_case) is not bool
        ):
            raise ValueError("choices must contain 1..256 unique bounded Unicode strings")

    def check(self, value: JSONValue, context: RuleContext) -> RuleResult:
        if isinstance(value, str) and value in self.choices:
            return RuleResult(True)
        matches = (
            [choice for choice in self.choices if choice.casefold() == value.casefold()]
            if self.fix_case and isinstance(value, str)
            else []
        )
        return RuleResult(
            False,
            "string_choice",
            "value is not an allowed string",
            matches[0] if len(matches) == 1 else _NO_FIX,
        )
