"""Closed, bounded interchange for the existing synchronous output pipeline."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from types import MappingProxyType
from typing import cast

from payload_palette.errors import PayloadValidationError, ValidationIssue
from payload_palette.ingress import decode_json_bytes
from payload_palette.output_schema import (
    JSONValue,
    OutputLimits,
    OutputPath,
    OutputSchema,
    _number,
    _schema_path_possible,
    _text,
    output_path,
)
from payload_palette.output_validation import (
    FailureAction,
    RuleBinding,
    StringChoices,
    TrimmedString,
    ValidationPipeline,
)
from payload_palette.schema_io import (
    SchemaDefinitionError,
    export_output_schema,
    load_output_schema,
)

_KIND = "payload-output-config"
_OUTPUT_LIMIT_NAMES = (
    "max_depth",
    "max_nodes",
    "max_characters",
    "max_issues",
    "max_invocations",
    "max_schema_steps",
)


@dataclass(frozen=True, slots=True)
class OutputConfigLimits:
    """Lowerable admission ceilings, separate from the configured output budgets.

    Depth counts edges from the root; nodes include containers and scalar values,
    while characters include every key and string. Shared children count again.
    The byte loader bounds raw bytes. Export bounds normalized ASCII JSON bytes.
    Every load additionally checks normalized interchange against default ceilings.
    """

    max_input_bytes: int = 4_000_000
    max_depth: int = 96
    max_nodes: int = 100_000
    max_characters: int = 1_000_000
    max_rules: int = 256
    max_path_segments: int = 8_192
    max_choice_values: int = 4_096

    def __post_init__(self) -> None:
        for name, minimum, ceiling in (
            ("max_input_bytes", 1, 4_000_000),
            ("max_depth", 0, 96),
            ("max_nodes", 1, 100_000),
            ("max_characters", 1, 1_000_000),
            ("max_rules", 0, 256),
            ("max_path_segments", 0, 8_192),
            ("max_choice_values", 0, 4_096),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= ceiling:
                raise ValueError(f"{name} must be an integer between {minimum} and {ceiling}")


class OutputConfigError(PayloadValidationError):
    """An unsupported, malformed, or oversized output configuration."""


def _error(code: str, message: str, path: OutputPath = ()) -> OutputConfigError:
    return OutputConfigError([ValidationIssue(code, message, output_path(path))])


def _limits(limits: OutputConfigLimits | None) -> OutputConfigLimits:
    if limits is None:
        return OutputConfigLimits()
    if type(limits) is not OutputConfigLimits:
        raise ValueError("limits must be OutputConfigLimits or None")
    limits.__post_init__()
    return limits


def _snapshot(document: object, limits: OutputConfigLimits) -> JSONValue:
    nodes = 0
    characters = 0
    active: set[int] = set()

    def charge(value: str, path: OutputPath) -> None:
        nonlocal characters
        characters += len(value)
        if characters > limits.max_characters:
            raise _error("config_budget", "configuration character budget exceeded", path)
        if not _text(value):
            raise _error("config_type", "configuration requires Unicode scalar strings", path)

    def visit(value: object, depth: int, path: OutputPath) -> JSONValue:
        nonlocal nodes
        nodes += 1
        if nodes > limits.max_nodes or depth > limits.max_depth:
            raise _error("config_budget", "configuration node or depth budget exceeded", path)
        if value is None or type(value) is bool or _number(value):
            return cast(JSONValue, value)
        if type(value) is str:
            charge(value, path)
            return value
        if type(value) not in (dict, list):
            raise _error("config_type", "configuration requires exact finite JSON values", path)
        if id(value) in active:
            raise _error("config_cycle", "cyclic configuration is not supported", path)
        if len(cast(list[object] | dict[object, object], value)) > limits.max_nodes - nodes:
            raise _error("config_budget", "configuration node budget exceeded", path)
        active.add(id(value))
        try:
            if type(value) is list:
                return [visit(child, depth + 1, (*path, i)) for i, child in enumerate(value)]
            result: dict[str, JSONValue] = {}
            for key, child in cast(dict[object, object], value).items():
                if type(key) is not str:
                    raise _error("config_type", "configuration keys must be exact strings", path)
                charge(key, path)
                result[key] = visit(child, depth + 1, (*path, key))
            return result
        finally:
            active.remove(id(value))

    return visit(document, 0, ())


def _object(
    value: JSONValue, allowed: set[str], required: set[str], path: OutputPath
) -> dict[str, JSONValue]:
    if type(value) is not dict:
        raise _error("config_type", "configuration section must be an object", path)
    if value.keys() - allowed:
        raise _error("config_keyword", "unsupported configuration keyword", path)
    if required - value.keys():
        raise _error("config_required", "required configuration field is absent", path)
    return value


def _compile(document: JSONValue, limits: OutputConfigLimits) -> ValidationPipeline:
    node = _object(
        document,
        {"kind", "version", "schema", "limits", "rules"},
        {"kind", "version", "schema"},
        (),
    )
    if type(node["kind"]) is not str or node["kind"] != _KIND:
        raise _error("config_kind", "unsupported configuration kind", ("kind",))
    if type(node["version"]) is not int or node["version"] != 1:
        raise _error("config_version", "unsupported configuration version", ("version",))
    raw_limits = _object(node.get("limits", {}), set(_OUTPUT_LIMIT_NAMES), set(), ("limits",))
    try:
        output_limits = OutputLimits(**cast(dict[str, int], raw_limits))
    except ValueError as exc:
        raise _error("config_constraint", "invalid output-work limits", ("limits",)) from exc
    try:
        schema = load_output_schema(node["schema"])
    except SchemaDefinitionError as exc:
        raise OutputConfigError(
            [
                ValidationIssue(issue.code, issue.message, '$["schema"]' + issue.path[1:])
                for issue in exc.issues
            ]
        ) from exc
    raw_rules = node.get("rules", [])
    if type(raw_rules) is not list:
        raise _error("config_type", "rules must be an array", ("rules",))
    if len(raw_rules) > limits.max_rules:
        raise _error("config_budget", "configuration rule budget exceeded", ("rules",))
    rules: list[RuleBinding] = []
    ids: set[str] = set()
    path_segments = 0
    choice_values = 0
    for index, raw_rule in enumerate(raw_rules):
        path: OutputPath = ("rules", index)
        rule = _object(
            raw_rule, {"id", "path", "validator", "on_fail"}, {"id", "path", "validator"}, path
        )
        raw_path = rule["path"]
        if type(raw_path) is not list or len(raw_path) > 32:
            raise _error(
                "config_path", "rule path must contain at most 32 segments", (*path, "path")
            )
        path_segments += len(raw_path)
        if path_segments > limits.max_path_segments:
            raise _error("config_budget", "aggregate rule path budget exceeded", (*path, "path"))
        validator_node = _object(
            rule["validator"], {"kind", "choices", "fix_case"}, {"kind"}, (*path, "validator")
        )
        validator: TrimmedString | StringChoices
        kind = validator_node["kind"]
        if kind == "trimmed_string":
            _object(validator_node, {"kind"}, {"kind"}, (*path, "validator"))
            validator = TrimmedString()
        elif kind == "string_choices":
            _object(
                validator_node,
                {"kind", "choices", "fix_case"},
                {"kind", "choices"},
                (*path, "validator"),
            )
            choices = validator_node["choices"]
            if type(choices) is not list or not 1 <= len(choices) <= 256:
                raise _error("config_constraint", "choices must contain 1..256 strings", path)
            choice_values += len(choices)
            if choice_values > limits.max_choice_values:
                raise _error("config_budget", "aggregate choice budget exceeded", path)
            try:
                validator = StringChoices(
                    tuple(cast(list[str], choices)),
                    cast(bool, validator_node.get("fix_case", False)),
                )
            except ValueError as exc:
                raise _error("config_constraint", "invalid string choices", path) from exc
        else:
            raise _error("config_validator", "unsupported validator kind", (*path, "validator"))
        try:
            binding = RuleBinding(
                cast(str, rule["id"]),
                tuple(cast(list[str | int], raw_path)),
                validator,
                cast(FailureAction, rule.get("on_fail", "reject")),
            )
        except ValueError as exc:
            raise _error("config_constraint", "invalid rule binding", path) from exc
        if binding.rule_id in ids:
            raise _error("config_duplicate_rule", "rule ids must be unique", (*path, "id"))
        ids.add(binding.rule_id)
        if not _schema_path_possible(schema, binding.path):
            raise _error("config_path", "rule path is incompatible with schema", (*path, "path"))
        rules.append(binding)
    return ValidationPipeline(schema, tuple(rules), output_limits)


def _check_schema(schema: OutputSchema) -> None:
    """Refuse foreign nested schemas before the trusted schema exporter is called."""
    stack = [(schema, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 4_096 or depth > 32:
            raise _error("config_budget", "expanded export schema exceeds limits", ("schema",))
        if type(current) is not OutputSchema:
            raise _error("config_type", "export requires exact OutputSchema nodes", ("schema",))
        if (
            type(current.properties) not in (dict, MappingProxyType)
            or type(current.required) is not tuple
            or type(current.any_of) is not tuple
            or type(current.kind) is not str
            or type(current.integer_mode) is not str
            or (current.enum is not None and type(current.enum) is not tuple)
            or (current.prefix_items is not None and type(current.prefix_items) is not tuple)
        ):
            raise _error("config_type", "malformed export schema", ("schema",))
        for name in ("minimum", "maximum", "min_length", "max_length"):
            value = getattr(current, name)
            if value is not None and not _number(value):
                raise _error("config_type", "malformed export schema constraint", ("schema",))
        if (
            any(not _text(key) for key in current.properties)
            or any(not _text(key) for key in current.required)
            or any(
                value is not None
                and type(value) is not bool
                and not _number(value)
                and not _text(value)
                for value in current.enum or ()
            )
        ):
            raise _error("config_type", "malformed export schema values", ("schema",))
        stack.extend((child, depth + 1) for child in current.properties.values())
        stack.extend((child, depth + 1) for child in current.any_of)
        stack.extend((child, depth + 1) for child in current.prefix_items or ())
        if current.items is not None:
            stack.append((current.items, depth + 1))
        if type(current.additional_properties) is not bool:
            stack.append((current.additional_properties, depth + 1))


def _document(pipeline: ValidationPipeline) -> dict[str, JSONValue]:
    if type(pipeline) is not ValidationPipeline or type(pipeline.limits) is not OutputLimits:
        raise _error("config_type", "export requires an exact synchronous pipeline and limits")
    if type(pipeline.rules) is not tuple or len(pipeline.rules) > 256:
        raise _error("config_type", "export requires at most 256 exact rule bindings")
    rules: list[JSONValue] = []
    for binding in pipeline.rules:
        if type(binding) is not RuleBinding or type(binding.path) is not tuple:
            raise _error("config_type", "export requires exact rule bindings and tuple paths")
        validator = binding.validator
        spec: dict[str, JSONValue]
        if type(validator) is TrimmedString:
            spec = {"kind": "trimmed_string"}
        elif type(validator) is StringChoices:
            if type(validator.choices) is not tuple or len(validator.choices) > 256:
                raise _error("config_type", "export requires bounded tuple choices")
            spec = {
                "kind": "string_choices",
                "choices": list(validator.choices),
                "fix_case": validator.fix_case,
            }
        else:
            raise _error("config_validator", "only exact built-in validators can be exported")
        rules.append(
            {
                "id": binding.rule_id,
                "path": list(binding.path),
                "validator": spec,
                "on_fail": binding.on_fail,
            }
        )
    _check_schema(pipeline.schema)
    return {
        "kind": _KIND,
        "version": 1,
        "schema": export_output_schema(pipeline.schema),
        "limits": {name: getattr(pipeline.limits, name) for name in _OUTPUT_LIMIT_NAMES},
        "rules": rules,
    }


def _canonical_chunks(document: JSONValue, maximum: int) -> Iterator[bytes]:
    encoder = json.JSONEncoder(
        ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":")
    )
    size = 0
    for chunk in encoder.iterencode(document):
        size += len(chunk)
        if size > maximum:
            raise _error("config_budget", "normalized configuration byte budget exceeded")
        yield chunk.encode("ascii")


def _normalized(pipeline: ValidationPipeline, limits: OutputConfigLimits) -> dict[str, JSONValue]:
    document = cast(dict[str, JSONValue], _snapshot(_document(pipeline), limits))
    _compile(document, limits)
    for _ in _canonical_chunks(document, limits.max_input_bytes):
        pass
    return document


def load_output_config(
    document: object, *, limits: OutputConfigLimits | None = None
) -> ValidationPipeline:
    """Compile owned exact JSON data without executing a validator or importing code.

    Normalized output must also fit all default interchange ceilings, even when
    the admitted input uses fewer fields or less expensive Unicode encoding.
    """
    active_limits = _limits(limits)
    pipeline = _compile(_snapshot(document, active_limits), active_limits)
    _normalized(pipeline, OutputConfigLimits())
    return pipeline


def load_output_config_json_bytes(
    payload: bytes | bytearray | memoryview, *, limits: OutputConfigLimits | None = None
) -> ValidationPipeline:
    """Strict bounded UTF-8 JSON ingress; decoding retains PayloadValidationError."""
    active_limits = _limits(limits)
    return load_output_config(
        decode_json_bytes(payload, max_input_bytes=active_limits.max_input_bytes),
        limits=active_limits,
    )


def export_output_config(
    pipeline: ValidationPipeline, *, limits: OutputConfigLimits | None = None
) -> dict[str, JSONValue]:
    """Export fresh normalized data for an exact, unmodified built-in pipeline.

    Live Python configuration is trusted: bypassing frozen-object invariants or
    concurrently mutating configuration internals is outside this API's contract.
    Unsupported validator types are refused without calling their attributes.
    """
    return _normalized(pipeline, _limits(limits))


def output_config_digest(
    pipeline: ValidationPipeline, *, limits: OutputConfigLimits | None = None
) -> str:
    """Return syntax-normalized SHA256 identity, not authentication or validation truth."""
    active_limits = _limits(limits)
    document = export_output_config(pipeline, limits=active_limits)
    digest = hashlib.sha256()
    for chunk in _canonical_chunks(document, active_limits.max_input_bytes):
        digest.update(chunk)
    return digest.hexdigest()
