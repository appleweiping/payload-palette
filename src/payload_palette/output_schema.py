"""Strict, bounded schemas for JSON model output (no implicit coercion)."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, TypeAlias, cast

from payload_palette.errors import ValidationIssue

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
SchemaKind: TypeAlias = Literal[
    "null", "boolean", "integer", "number", "string", "array", "object", "union"
]
OutputPath: TypeAlias = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class OutputLimits:
    """Bounds on snapshots, schema errors, and semantic validator work.

    Validators are trusted Python code: call count is bounded, but execution
    inside a callback cannot be preempted or sandboxed by this synchronous API.
    """

    max_depth: int = 32
    max_nodes: int = 10_000
    max_characters: int = 1_000_000
    max_issues: int = 100
    max_invocations: int = 1_000
    max_schema_steps: int = 100_000

    def __post_init__(self) -> None:
        for name, ceiling in (
            ("max_depth", 64),
            ("max_nodes", 100_000),
            ("max_characters", 8_000_000),
            ("max_issues", 1_000),
            ("max_invocations", 10_000),
            ("max_schema_steps", 1_000_000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be an integer between 1 and {ceiling}")


class OutputContractError(ValueError):
    """A non-JSON value or resource limit prevents safe validation."""


@dataclass(slots=True)
class _SchemaWork:
    """Private monotonic counter for composed validation of owned snapshots."""

    steps: int = 0


def _text(value: object) -> bool:
    return type(value) is str and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)


def _number(value: object) -> bool:
    return (type(value) is int and value.bit_length() <= 850) or (
        type(value) is float and math.isfinite(value)
    )


def snapshot_json(value: object, limits: OutputLimits) -> JSONValue:
    """Copy exact built-in JSON containers without invoking user copy hooks."""

    nodes = 0
    characters = 0
    active: set[int] = set()

    def visit(current: object, depth: int) -> JSONValue:
        nonlocal nodes, characters
        nodes += 1
        if nodes > limits.max_nodes or depth > limits.max_depth:
            raise OutputContractError("JSON node or depth limit exceeded")
        if current is None or type(current) is bool or _number(current):
            return cast(JSONScalar, current)
        if type(current) is str:
            characters += len(current)
            if characters > limits.max_characters or not _text(current):
                raise OutputContractError("JSON character limit or Unicode contract violated")
            return current
        if type(current) not in (list, dict):
            raise OutputContractError("values must be finite JSON scalars or built-in lists/dicts")
        if id(current) in active:
            raise OutputContractError("cyclic JSON container")
        active.add(id(current))
        try:
            if type(current) is list:
                if len(current) > limits.max_nodes - nodes:
                    raise OutputContractError("JSON node limit exceeded")
                return [visit(child, depth + 1) for child in current]
            mapping = cast(dict[object, object], current)
            if len(mapping) > limits.max_nodes - nodes:
                raise OutputContractError("JSON node limit exceeded")
            result: dict[str, JSONValue] = {}
            for key, child in mapping.items():
                if type(key) is not str:
                    raise OutputContractError("JSON object keys must be Unicode scalar strings")
                string_key = key
                characters += len(string_key)
                if characters > limits.max_characters:
                    raise OutputContractError("JSON character limit exceeded")
                if not _text(string_key):
                    raise OutputContractError("JSON object keys must be Unicode scalar strings")
                result[string_key] = visit(child, depth + 1)
            return result
        finally:
            active.remove(id(current))

    return visit(value, 0)


def output_path(path: OutputPath) -> str:
    """Render an unambiguous field path; object keys are always JSON quoted."""

    return "$" + "".join(f"[{json.dumps(part, ensure_ascii=True)}]" for part in path)


@dataclass(frozen=True, slots=True)
class OutputSchema:
    """A strict JSON schema subset with immutable nested configuration.

    Supported features: the seven JSON types, nested object properties and
    homogeneous arrays, required/extra keys, scalar enums, numeric bounds, and
    string/array length bounds. This is not a full JSON Schema interpreter.
    """

    kind: SchemaKind
    properties: Mapping[str, OutputSchema] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    items: OutputSchema | None = None
    additional_properties: bool | OutputSchema = False
    minimum: int | float | None = None
    maximum: int | float | None = None
    min_length: int | None = None
    max_length: int | None = None
    enum: tuple[JSONScalar, ...] | None = None
    any_of: tuple[OutputSchema, ...] = ()
    integer_mode: Literal["strict", "json"] = "strict"

    def __post_init__(self) -> None:
        if type(self.integer_mode) is not str or self.integer_mode not in ("strict", "json"):
            raise ValueError("integer_mode must be strict or json")
        if self.kind != "integer" and self.integer_mode != "strict":
            raise ValueError("integer_mode=json requires an integer schema")
        if self.kind not in (
            "null",
            "boolean",
            "integer",
            "number",
            "string",
            "array",
            "object",
            "union",
        ):
            raise ValueError("unsupported schema kind")
        if type(self.any_of) is not tuple or any(
            type(branch) is not OutputSchema for branch in self.any_of
        ):
            raise ValueError("any_of must be a tuple of OutputSchema branches")
        if (self.kind == "union" and not 2 <= len(self.any_of) <= 16) or (
            self.kind != "union" and self.any_of
        ):
            raise ValueError("only union schemas require 2..16 any_of branches")
        if type(self.properties) not in (dict, MappingProxyType) or len(self.properties) > 256:
            raise ValueError("properties must be a mapping with at most 256 entries")
        if any(
            not _text(key) or len(key) > 256 or type(child) is not OutputSchema
            for key, child in self.properties.items()
        ):
            raise ValueError("invalid property name or schema")
        object.__setattr__(self, "properties", MappingProxyType(dict(self.properties)))
        if (
            type(self.required) is not tuple
            or len(self.required) > 256
            or any(type(key) is not str or key not in self.properties for key in self.required)
            or len(set(self.required)) != len(self.required)
            or type(self.additional_properties) not in (bool, OutputSchema)
        ):
            raise ValueError("required must contain unique declared property names")
        if self.kind != "object" and (
            self.properties or self.required or self.additional_properties
        ):
            raise ValueError("object constraints require an object schema")
        if (self.kind == "array" and type(self.items) is not OutputSchema) or (
            self.kind != "array" and self.items is not None
        ):
            raise ValueError("only array schemas require an items schema")
        for bound in (self.minimum, self.maximum):
            if bound is not None and (self.kind not in ("number", "integer") or not _number(bound)):
                raise ValueError("numeric bounds require finite numbers and a numeric schema")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum exceeds maximum")
        for length in (self.min_length, self.max_length):
            if length is not None and (
                self.kind not in ("string", "array")
                or type(length) is not int
                or not 0 <= length <= 8_000_000
            ):
                raise ValueError(
                    "length bounds require nonnegative integers and string/array schema"
                )
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length exceeds max_length")
        if self.enum is not None and (
            type(self.enum) is not tuple
            or not 1 <= len(self.enum) <= 256
            or self.kind in ("object", "array", "union")
            or any(type(item) is str and len(item) > 2_048 for item in self.enum)
            or any(not self._matches(item) for item in self.enum)
        ):
            raise ValueError("enum must contain 1..256 scalar values of the schema type")
        # Bound expanded schema work, including repeated shared child schemas.
        stack = [(self, 0)]
        count = 0
        characters = 0
        while stack:
            schema, depth = stack.pop()
            count += 1
            if depth > 32 or count > 4_096:
                raise ValueError("schema exceeds the 32-depth or 4096-node limit")
            characters += sum(len(key) for key in schema.properties)
            if schema.enum is not None:
                characters += sum(len(item) for item in schema.enum if type(item) is str)
            if characters > 1_000_000:
                raise ValueError("schema exceeds the 1000000-character limit")
            stack.extend((child, depth + 1) for child in schema.properties.values())
            if schema.items is not None:
                stack.append((schema.items, depth + 1))
            if type(schema.additional_properties) is OutputSchema:
                stack.append((schema.additional_properties, depth + 1))
            stack.extend((branch, depth + 1) for branch in schema.any_of)

    def nullable(self) -> OutputSchema:
        """Return a schema accepting this contract or JSON null."""

        if self.kind == "null" or any(branch.kind == "null" for branch in self.any_of):
            return self
        return OutputSchema("union", any_of=(self, OutputSchema("null")))

    def _matches(self, value: object) -> bool:
        return {
            "null": value is None,
            "boolean": type(value) is bool,
            "integer": (type(value) is int and _number(value))
            or (
                self.integer_mode == "json"
                and type(value) is float
                and math.isfinite(value)
                and value.is_integer()
            ),
            "number": _number(value),
            "string": _text(value),
            "array": type(value) is list,
            "object": type(value) is dict,
        }[self.kind]

    def validate(
        self, value: object, *, limits: OutputLimits | None = None
    ) -> tuple[ValidationIssue, ...]:
        """Return structural errors, with one explicit truncation issue at the cap."""

        active_limits = _limits(limits)
        document = snapshot_json(value, active_limits)
        return self._validate_snapshot(document, active_limits, _SchemaWork())

    def _validate_snapshot(
        self,
        document: JSONValue,
        active_limits: OutputLimits,
        work: _SchemaWork,
        path: OutputPath = (),
    ) -> tuple[ValidationIssue, ...]:
        """Validate an already owned snapshot without resetting shared schema work."""
        issues: list[ValidationIssue] = []
        truncated = False

        def issue(code: str, message: str, path: OutputPath) -> None:
            nonlocal truncated
            if len(issues) < active_limits.max_issues:
                issues.append(ValidationIssue(code, message, output_path(path)))
            else:
                truncated = True

        def visit(schema: OutputSchema, current: JSONValue, path: OutputPath) -> None:
            nonlocal issues, truncated
            if truncated:
                return
            work.steps += 1
            if work.steps > active_limits.max_schema_steps:
                raise OutputContractError("schema evaluation work limit exceeded")
            if schema.kind == "union":
                original_issues, original_truncated = issues, truncated
                for branch in schema.any_of:
                    issues, truncated = [], False
                    try:
                        visit(branch, current, path)
                        matched = not issues
                    finally:
                        issues, truncated = original_issues, original_truncated
                    if matched:
                        return
                issue("schema_any_of", "value does not satisfy any union branch", path)
                return
            if not schema._matches(current):
                issue("schema_type", f"expected {schema.kind}", path)
                return
            if schema.enum is not None and not any(
                (
                    schema.kind == "number"
                    or schema.integer_mode == "json"
                    or type(current) is type(member)
                )
                and current == member
                for member in schema.enum
            ):
                issue("schema_enum", "value is not a declared enum member", path)
            if type(current) in (int, float):
                number = cast(int | float, current)
                if schema.minimum is not None and number < schema.minimum:
                    issue("schema_minimum", "value is below minimum", path)
                if schema.maximum is not None and number > schema.maximum:
                    issue("schema_maximum", "value is above maximum", path)
            if isinstance(current, (str, list)):
                if schema.min_length is not None and len(current) < schema.min_length:
                    issue("schema_min_length", "value is shorter than min_length", path)
                if schema.max_length is not None and len(current) > schema.max_length:
                    issue("schema_max_length", "value is longer than max_length", path)
            if isinstance(current, dict):
                for key in schema.required:
                    if key not in current:
                        issue("schema_required", "required property is absent", (*path, key))
                for key, child in current.items():
                    if key in schema.properties:
                        visit(schema.properties[key], child, (*path, key))
                    elif type(schema.additional_properties) is OutputSchema:
                        visit(schema.additional_properties, child, (*path, key))
                    elif not schema.additional_properties:
                        issue("schema_extra", "undeclared property", (*path, key))
            elif isinstance(current, list) and schema.items is not None:
                for index, child in enumerate(current):
                    visit(schema.items, child, (*path, index))

        visit(self, document, path)
        if truncated:
            issues.append(ValidationIssue("schema_issue_limit", "further errors omitted", "$"))
        return tuple(issues)

    def json_schema(self, *, preserve_python_types: bool = False) -> dict[str, JSONValue]:
        """Export a portable projection, or preserve strict integers with an extension.

        Standard JSON Schema integer accepts integral floats. The default portable
        projection cannot preserve Python int-vs-float representation strictness.
        Interchange callers can request the x-payload-strict-integer extension.
        """

        if type(preserve_python_types) is not bool:
            raise ValueError("preserve_python_types must be a boolean")

        if self.kind == "union":
            return {
                "anyOf": [
                    branch.json_schema(preserve_python_types=preserve_python_types)
                    for branch in self.any_of
                ]
            }
        result: dict[str, JSONValue] = {"type": self.kind}
        if self.kind == "integer" and self.integer_mode == "strict" and preserve_python_types:
            result["x-payload-strict-integer"] = True
        if self.kind == "object":
            result.update(
                properties={
                    key: child.json_schema(preserve_python_types=preserve_python_types)
                    for key, child in self.properties.items()
                },
                required=list(self.required),
                additionalProperties=(
                    self.additional_properties.json_schema(
                        preserve_python_types=preserve_python_types
                    )
                    if type(self.additional_properties) is OutputSchema
                    else cast(bool, self.additional_properties)
                ),
            )
        if self.items is not None:
            result["items"] = self.items.json_schema(preserve_python_types=preserve_python_types)
        for keyword, value in (("minimum", self.minimum), ("maximum", self.maximum)):
            if value is not None:
                result[keyword] = value
        suffix = "Items" if self.kind == "array" else "Length"
        if self.min_length is not None:
            result[f"min{suffix}"] = self.min_length
        if self.max_length is not None:
            result[f"max{suffix}"] = self.max_length
        if self.enum is not None:
            # Direct constructors historically allow repeated enum members. Emit
            # unique JSON-schema choices without changing their acceptance set.
            unique: list[JSONValue] = []
            for member in self.enum:
                if member not in unique:
                    unique.append(member)
            result["enum"] = unique
        return result


def _limits(limits: OutputLimits | None) -> OutputLimits:
    if limits is not None and type(limits) is not OutputLimits:
        raise ValueError("limits must be OutputLimits or None")
    return OutputLimits() if limits is None else limits


def _schema_path_possible(
    schema: OutputSchema, path: OutputPath, *, allow_dynamic: bool = True
) -> bool:
    """Check existence in at least one union alternative without inspecting output values."""

    if not path:
        return True
    if schema.kind == "union":
        return any(
            _schema_path_possible(branch, path, allow_dynamic=allow_dynamic)
            for branch in schema.any_of
        )
    segment, remaining = path[0], path[1:]
    if schema.kind == "object" and type(segment) is str:
        if segment in schema.properties:
            return _schema_path_possible(
                schema.properties[segment], remaining, allow_dynamic=allow_dynamic
            )
        if not allow_dynamic:
            return False
        if type(schema.additional_properties) is OutputSchema:
            return _schema_path_possible(
                schema.additional_properties, remaining, allow_dynamic=allow_dynamic
            )
        return cast(bool, schema.additional_properties)
    if schema.kind == "array" and type(segment) is int and segment >= 0:
        if schema.max_length is not None and segment >= schema.max_length:
            return False
        return schema.items is not None and _schema_path_possible(
            schema.items, remaining, allow_dynamic=allow_dynamic
        )
    return False
