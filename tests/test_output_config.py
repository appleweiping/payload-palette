from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, fields, replace
from typing import Any

import pytest

import payload_palette
from payload_palette import (
    OutputConfigError,
    OutputConfigLimits,
    OutputLimits,
    OutputSchema,
    PayloadValidationError,
    RuleBinding,
    RuleContext,
    RuleResult,
    StringChoices,
    TrimmedString,
    ValidationPipeline,
    export_output_config,
    load_output_config,
    load_output_config_json_bytes,
    output_config_digest,
)


@pytest.mark.parametrize(
    "name",
    [
        "OutputConfigError",
        "OutputConfigLimits",
        "load_output_config",
        "load_output_config_json_bytes",
        "export_output_config",
        "output_config_digest",
    ],
)
def test_configuration_public_api_is_available(name: str) -> None:
    assert name in payload_palette.__all__
    assert hasattr(payload_palette, name)


def config(schema: Any = None, rules: Any = None, **changes: Any) -> dict[str, Any]:
    return {
        "kind": "payload-output-config",
        "version": 1,
        "schema": {"type": "string"} if schema is None else schema,
        **({"rules": rules} if rules is not None else {}),
        **changes,
    }


def rule(validator: Any = None, **changes: Any) -> dict[str, Any]:
    return {
        "id": "trim",
        "path": [],
        "validator": {"kind": "trimmed_string"} if validator is None else validator,
        **changes,
    }


def graph_counts(value: Any) -> tuple[int, int, int]:
    """Independent tree accounting: nodes, maximum edge depth, key/string characters."""
    if isinstance(value, (dict, list)):
        values = value.values() if isinstance(value, dict) else value
        children = [graph_counts(child) for child in values]
        return (
            1 + sum(child[0] for child in children),
            max((1 + child[1] for child in children), default=0),
            sum(child[2] for child in children)
            + (sum(len(key) for key in value) if isinstance(value, dict) else 0),
        )
    return 1, 0, len(value) if isinstance(value, str) else 0


def canonical(document: Any) -> bytes:
    return json.dumps(
        document, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode("ascii")


def test_defaults_compile_existing_engine_and_export_explicit_owned_configuration() -> None:
    pipeline = load_output_config(config({"type": "object"}))
    assert type(pipeline) is ValidationPipeline
    assert pipeline.validate({"unknown": [None, True]}).valid
    exported = export_output_config(pipeline)
    assert exported == {
        "kind": "payload-output-config",
        "version": 1,
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": True,
        },
        "limits": {item.name: getattr(OutputLimits(), item.name) for item in fields(OutputLimits)},
        "rules": [],
    }
    exported.clear()
    assert pipeline.validate({}).valid
    assert load_output_config_json_bytes(canonical(export_output_config(pipeline))) == pipeline


@pytest.mark.parametrize("bad", [None, True, [], 1, "config", {}])
def test_top_level_must_be_closed_required_object(bad: Any) -> None:
    with pytest.raises(OutputConfigError):
        load_output_config(bad)


@pytest.mark.parametrize(
    "document",
    [
        config(kind=True),
        config(kind="module:call"),
        config(version=True),
        config(version=1.0),
        config(version="1"),
        config(version=0),
        config(version=2),
        config(extra=None),
        config(limits=None),
        config(limits=[]),
        config(limits={"unknown": 1}),
        config(limits={"max_depth": True}),
        config(limits={"max_issues": 0}),
        config(limits={"max_nodes": 100_001}),
        config({"type": "string", "$ref": "https://secret.invalid/schema"}),
        config({"type": "string", "minimum": 0}),
        config(rules={}),
        config(rules=[None]),
        config(rules=[{}]),
        config(rules=[rule(extra=True)]),
        config(rules=[rule(id="")]),
        config(rules=[rule(id="Bad")]),
        config(rules=[rule(id=True)]),
        config(rules=[rule(id="a" * 65)]),
        config(rules=[rule(path="$")]),
        config(rules=[rule(path=[True])]),
        config(rules=[rule(path=[-1])]),
        config(rules=[rule(path=[1.0])]),
        config(rules=[rule(path=[100_000])]),
        config(rules=[rule(path=["x" * 257])]),
        config(rules=[rule(path=["x"] * 33)]),
        config(rules=[rule(on_fail=True)]),
        config(rules=[rule(on_fail="reask")]),
        config(rules=[rule(on_fail="filter")]),
        config(rules=[rule([])]),
        config(rules=[rule({})]),
        config(rules=[rule({"kind": "module:callback"})]),
        config(rules=[rule({"kind": []})]),
        config(rules=[rule({"kind": "trimmed_string", "choices": ["x"]})]),
        config(rules=[rule({"kind": "trimmed_string", "fix_case": False})]),
        config(rules=[rule({"kind": "string_choices"})]),
        config(rules=[rule({"kind": "string_choices", "choices": "x"})]),
        config(rules=[rule({"kind": "string_choices", "choices": []})]),
        config(rules=[rule({"kind": "string_choices", "choices": ["x", "x"]})]),
        config(rules=[rule({"kind": "string_choices", "choices": [True]})]),
        config(rules=[rule({"kind": "string_choices", "choices": ["x" * 2049]})]),
        config(rules=[rule({"kind": "string_choices", "choices": ["x"], "fix_case": 1})]),
        config(rules=[rule(), rule()]),
    ],
)
def test_independent_invalid_configuration_vectors(document: Any) -> None:
    with pytest.raises(OutputConfigError):
        load_output_config(document)


def test_stable_configuration_errors_and_nested_schema_paths() -> None:
    with pytest.raises(OutputConfigError) as caught:
        load_output_config(config(rules=[rule(), rule()]))
    assert caught.value.to_dict() == {
        "valid": False,
        "error_count": 1,
        "errors": [
            {
                "code": "config_duplicate_rule",
                "message": "rule ids must be unique",
                "path": '$["rules"][1]["id"]',
            }
        ],
    }
    with pytest.raises(OutputConfigError) as caught:
        load_output_config(config({"type": "object", "properties": {"field": {"$ref": "secret"}}}))
    assert caught.value.issues[0].path == '$["schema"]["properties"]["field"]'
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        ({"type": "string"}, ["x"]),
        ({"type": "object", "additionalProperties": False}, ["typo"]),
        ({"type": "object"}, [0]),
        ({"type": "array", "items": {"type": "string"}}, ["0"]),
        ({"type": "array", "items": {"type": "string"}, "maxItems": 1}, [1]),
        ({"type": "array", "items": False}, [0]),
        ({"type": "object", "additionalProperties": {"type": "string"}}, ["x", "y"]),
    ],
)
def test_impossible_paths_fail_at_configuration_time(schema: Any, path: Any) -> None:
    with pytest.raises(OutputConfigError, match="config_path"):
        load_output_config(config(schema, [rule(path=path)]))


def test_nested_positional_union_and_absent_paths_keep_existing_semantics() -> None:
    schema = {
        "anyOf": [
            {"type": "null"},
            {"type": "array", "prefixItems": [{"type": "string"}], "items": False},
        ]
    }
    pipeline = load_output_config(config(schema, [rule(path=[0], on_fail="fix")]))
    assert pipeline.validate([" x "]).output == ["x"]
    assert pipeline.validate([]).invocations == 0
    assert pipeline.validate(None).invocations == 0
    assert not pipeline.validate(["x", "y"]).valid
    dynamic = load_output_config(config({"type": "object"}, [rule(path=["x", 0])]))
    assert dynamic.validate({}).invocations == 0
    assert dynamic.validate({"x": ["x"]}).invocations == 1


def test_typed_maps_homogeneous_arrays_and_schema_order_roundtrip() -> None:
    schema = OutputSchema(
        "object",
        additional_properties=OutputSchema(
            "array",
            items=OutputSchema("number", minimum=-1, maximum=2, enum=(-1, 1.5, 2)),
            min_length=1,
            max_length=2,
        ),
    )
    direct = ValidationPipeline(schema)
    restored = load_output_config_json_bytes(canonical(export_output_config(direct)))
    for value in ({"x": [-1, 1.5]}, {"x": []}, {"x": [True]}, {"x": [2, 2, 2]}):
        assert restored.validate(value) == direct.validate(value)
    union = ValidationPipeline(
        OutputSchema(
            "union",
            any_of=(
                OutputSchema("string", enum=("x",)),
                OutputSchema("null", enum=(None,)),
                OutputSchema("boolean", enum=(True,)),
            ),
        )
    )
    assert [
        branch.kind for branch in load_output_config(export_output_config(union)).schema.any_of
    ] == [
        "string",
        "null",
        "boolean",
    ]


def test_independent_per_rule_upper_boundaries_and_ambiguous_case_repair() -> None:
    identifier = "a" * 64
    key = "x" * 256
    choices = [str(i).zfill(2048) for i in range(256)]
    pipeline = load_output_config(
        config(
            {"type": "object"},
            [
                rule({"kind": "string_choices", "choices": choices}, id=identifier, path=[key]),
            ],
        )
    )
    assert pipeline.validate({key: choices[0]}).valid
    indexed = load_output_config(
        config({"type": "array", "items": {"type": "string"}}, [rule(path=[99_999])])
    )
    assert indexed.validate([]).invocations == 0
    with pytest.raises(OutputConfigError):
        load_output_config(
            config(
                {"type": "array", "items": {"type": "string"}}, [rule(path=[0], on_fail="filter")]
            )
        )
    for options, valid, calls in ((["SS"], True, 3), (["SS", "ss"], False, 1)):
        pipeline = load_output_config(
            config(
                rules=[
                    rule(
                        {"kind": "string_choices", "choices": options, "fix_case": True},
                        on_fail="fix",
                    )
                ]
            )
        )
        assert pipeline.validate("ß").valid is valid
        assert pipeline.validate("ß").invocations == calls


def test_repair_filter_complete_report_matches_handwritten_oracle_and_direct_pipeline() -> None:
    schema = OutputSchema(
        "object",
        properties={"label": OutputSchema("string"), "note": OutputSchema("string")},
        required=("label",),
    )
    direct = ValidationPipeline(
        schema,
        (
            RuleBinding("trim", ("label",), TrimmedString(), "fix"),
            RuleBinding("choose", ("label",), StringChoices(("A",), True), "fix"),
            RuleBinding("drop", ("note",), TrimmedString(), "filter"),
        ),
    )
    document = config(
        {
            "type": "object",
            "properties": {"label": {"type": "string"}, "note": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
        [
            rule(path=["label"], on_fail="fix"),
            rule(
                {"kind": "string_choices", "choices": ["A"], "fix_case": True},
                id="choose",
                path=["label"],
                on_fail="fix",
            ),
            rule(id="drop", path=["note"], on_fail="filter"),
        ],
    )
    original = {"label": " a ", "note": " secret "}
    result = load_output_config(document).validate(original)
    assert result == direct.validate(original)
    assert result.valid and result.issues == () and result.output == {"label": "A"}
    assert result.invocations == 7
    assert [(item.rule_id, item.phase, item.status, item.code) for item in result.outcomes] == [
        ("trim", "repair", "fixed", "edge_whitespace"),
        ("choose", "repair", "fixed", "string_choice"),
        ("drop", "initial", "filtered", "edge_whitespace"),
        ("trim", "final", "passed", ""),
        ("choose", "final", "passed", ""),
    ]
    assert original == {"label": " a ", "note": " secret "}
    assert "output" not in result.to_dict()


def test_later_repair_required_filter_and_null_oracles() -> None:
    lower = rule({"kind": "string_choices", "choices": ["a"]}, id="lower")
    upper = rule(
        {"kind": "string_choices", "choices": ["A"], "fix_case": True}, id="upper", on_fail="fix"
    )
    pipeline = load_output_config(config(rules=[lower, upper]))
    direct = ValidationPipeline(
        OutputSchema("string"),
        (
            RuleBinding("lower", (), StringChoices(("a",))),
            RuleBinding("upper", (), StringChoices(("A",), True), "fix"),
        ),
    )
    result = pipeline.validate("a")
    assert result == direct.validate("a")
    assert not result.valid and result.output is None and result.invocations == 5
    assert [item.status for item in result.outcomes] == ["passed", "fixed", "rejected", "passed"]
    assert result.issues[0].code == "string_choice"
    schema = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
        "additionalProperties": False,
    }
    filtered = load_output_config(config(schema, [rule(path=["label"], on_fail="filter")]))
    expected = ValidationPipeline(
        OutputSchema("object", properties={"label": OutputSchema("string")}, required=("label",)),
        (RuleBinding("trim", ("label",), TrimmedString(), "filter"),),
    ).validate({"label": " a "})
    report = filtered.validate({"label": " a "})
    assert report == expected
    assert not report.valid and report.invocations == 1 and report.output is None
    assert report.issues[0].code == "schema_required"
    null = load_output_config(config({"type": "null"})).validate(None)
    assert null == ValidationPipeline(OutputSchema("null")).validate(None)
    assert null.to_dict(include_output=True) == {
        "valid": True,
        "issues": [],
        "outcomes": [],
        "invocations": 0,
        "output": None,
    }


def test_output_work_budgets_and_initial_schema_rejection_are_not_widened() -> None:
    pipeline = load_output_config(
        config(rules=[rule(on_fail="fix")], limits={"max_invocations": 2})
    )
    result = pipeline.validate(" x ")
    assert not result.valid and result.invocations == 2
    assert result.issues[0].code == "validator_budget"
    assert pipeline.validate(7).invocations == 0


def test_owned_import_export_and_normalized_digest() -> None:
    document = config(rules=[rule({"kind": "string_choices", "choices": ["é", "😀"]})])
    pipeline = load_output_config(document)
    digest = output_config_digest(pipeline)
    exported = export_output_config(pipeline)
    assert digest == hashlib.sha256(canonical(exported)).hexdigest()
    assert len(digest) == 64
    assert digest == output_config_digest(load_output_config(exported))
    assert digest == output_config_digest(load_output_config_json_bytes(canonical(exported)))
    assert digest == output_config_digest(
        load_output_config(dict(reversed(list(document.items()))))
    )
    document["rules"][0]["validator"]["choices"].clear()
    exported["rules"][0]["validator"]["choices"].clear()  # type: ignore[index,union-attr]
    assert pipeline.validate("😀").valid
    assert output_config_digest(pipeline) == digest
    with pytest.raises(FrozenInstanceError):
        pipeline.rules = ()  # type: ignore[misc]
    explicit = config(
        rules=[rule(on_fail="reject")],
        limits={item.name: getattr(OutputLimits(), item.name) for item in fields(OutputLimits)},
    )
    assert output_config_digest(load_output_config(explicit)) == output_config_digest(
        load_output_config(config(rules=[rule()]))
    )


def test_collection_order_and_integer_representation_survive_interchange() -> None:
    rules = [rule(id="a"), rule(id="b")]
    assert output_config_digest(load_output_config(config(rules=rules))) != output_config_digest(
        load_output_config(config(rules=list(reversed(rules))))
    )
    for options in (["a", "b"], ["b", "a"]):
        loaded = load_output_config(
            config(rules=[rule({"kind": "string_choices", "choices": options})])
        )
        assert export_output_config(loaded)["rules"][0]["validator"]["choices"] == options  # type: ignore[index]
    strict = ValidationPipeline(OutputSchema("integer"))
    restored = load_output_config(export_output_config(strict))
    assert restored.validate(1).valid and not restored.validate(1.0).valid
    imported = load_output_config(config({"type": "integer"}))
    assert imported.validate(1.0).valid and not imported.validate(True).valid
    assert output_config_digest(strict) != output_config_digest(imported)
    ordered = load_output_config(
        config(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["b", "a"],
            }
        )
    )
    assert [issue.path for issue in ordered.validate({}).issues] == ['$["b"]', '$["a"]']
    assert load_output_config_json_bytes(
        canonical(export_output_config(ordered))
    ).schema.required == ("b", "a")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "invalid_json"),
        (b"\xff", "input_encoding"),
        (b'{"kind":"secret","kind":"again"}', "duplicate_json_key"),
        (b'{"value":NaN}', "nonstandard_json_number"),
        (b'{"value":1e999}', "json_number_range"),
        (b'{"value":"\\ud800"}', "invalid_unicode"),
    ],
)
def test_strict_byte_ingress_retains_existing_exception_codes(payload: bytes, code: str) -> None:
    with pytest.raises(PayloadValidationError) as caught:
        load_output_config_json_bytes(payload)
    assert caught.value.issues[0].code == code


def test_raw_byte_limit_and_normalized_byte_limit_exact_boundaries() -> None:
    raw = canonical(config())
    policy = OutputConfigLimits(max_input_bytes=len(raw))
    pipeline = load_output_config_json_bytes(raw, limits=policy)
    for view in (bytearray(raw), memoryview(raw)):
        assert load_output_config_json_bytes(view, limits=policy) == pipeline
    with pytest.raises(PayloadValidationError, match="input_too_large"):
        load_output_config_json_bytes(raw + b" ", limits=policy)
    normalized = canonical(export_output_config(pipeline))
    assert export_output_config(pipeline, limits=replace(policy, max_input_bytes=len(normalized)))
    with pytest.raises(OutputConfigError, match="normalized configuration byte"):
        export_output_config(pipeline, limits=replace(policy, max_input_bytes=len(normalized) - 1))


@pytest.mark.parametrize("name", [item.name for item in fields(OutputConfigLimits)])
@pytest.mark.parametrize("value", [True, 1.0, "1", -1, None])
def test_limit_exact_types_and_lower_bounds(name: str, value: Any) -> None:
    with pytest.raises(ValueError):
        OutputConfigLimits(**{name: value})


@pytest.mark.parametrize("name", [item.name for item in fields(OutputConfigLimits)])
def test_limit_hard_ceilings(name: str) -> None:
    with pytest.raises(ValueError):
        OutputConfigLimits(**{name: getattr(OutputConfigLimits(), name) + 1})


@pytest.mark.parametrize("name,index", [("max_nodes", 0), ("max_depth", 1), ("max_characters", 2)])
def test_independent_graph_budget_boundaries(name: str, index: int) -> None:
    document = config()
    size = graph_counts(document)[index]
    assert load_output_config(document, limits=OutputConfigLimits(**{name: size}))
    with pytest.raises(OutputConfigError, match="config_budget"):
        load_output_config(document, limits=OutputConfigLimits(**{name: size - 1}))
    pipeline = load_output_config(document)
    expanded = graph_counts(export_output_config(pipeline))[index]
    assert export_output_config(pipeline, limits=OutputConfigLimits(**{name: expanded}))
    with pytest.raises(OutputConfigError, match="config_budget"):
        export_output_config(pipeline, limits=OutputConfigLimits(**{name: expanded - 1}))


def test_rules_paths_choices_aggregate_boundaries() -> None:
    rules = [rule(id=f"r{i}", path=["x"] * 32) for i in range(256)]
    document = config({"type": "object"}, rules)
    assert len(load_output_config(document).rules) == 256
    for policy in (OutputConfigLimits(max_rules=255), OutputConfigLimits(max_path_segments=8191)):
        with pytest.raises(OutputConfigError, match="config_budget"):
            load_output_config(document, limits=policy)
    with pytest.raises(OutputConfigError, match="config_budget"):
        load_output_config(config({"type": "object"}, [*rules, rule(id="extra")]))
    choices = [
        rule({"kind": "string_choices", "choices": [str(i) for i in range(256)]}, id=f"r{j}")
        for j in range(16)
    ]
    assert len(load_output_config(config(rules=choices)).rules) == 16
    with pytest.raises(OutputConfigError, match="aggregate choice"):
        load_output_config(config(rules=choices), limits=OutputConfigLimits(max_choice_values=4095))
    with pytest.raises(OutputConfigError, match="aggregate choice"):
        load_output_config(
            config(rules=[*choices, rule({"kind": "string_choices", "choices": ["x"]})])
        )
    assert load_output_config(
        config(), limits=OutputConfigLimits(max_rules=0, max_path_segments=0, max_choice_values=0)
    )


def test_maximum_schema_depth_exceeds_output_snapshot_depth_but_roundtrips() -> None:
    schema: Any = {"type": "string"}
    value: Any = "x"
    for _ in range(32):
        schema = {"type": "object", "properties": {"x": schema}, "required": ["x"]}
        value = {"x": value}
    document = config(schema)
    assert graph_counts(document)[1] > 64
    pipeline = load_output_config(document)
    assert pipeline.validate(value).valid
    assert (
        load_output_config_json_bytes(canonical(export_output_config(pipeline)))
        .validate(value)
        .valid
    )
    with pytest.raises(OutputConfigError):
        load_output_config(config({"type": "object", "properties": {"x": schema}}))


def test_ascii_expansion_cannot_break_default_byte_interchange() -> None:
    rules = [
        rule(
            {"kind": "string_choices", "choices": ["😀" * 128 + str(i) for i in range(256)]},
            id=f"r{j}",
        )
        for j in range(16)
    ]
    document = config(rules=rules)
    raw = json.dumps(document, ensure_ascii=False).encode("utf-8")
    assert len(raw) < 4_000_000 and graph_counts(document)[2] < 1_000_000
    with pytest.raises(OutputConfigError, match="normalized configuration byte"):
        load_output_config_json_bytes(raw)


def test_default_field_expansion_cannot_break_default_graph_interchange() -> None:
    leaf = {"type": "integer", "enum": list(range(256))}
    document = config(
        {
            "type": "object",
            "properties": {
                "a": {"type": "object", "properties": {f"p{i}": leaf for i in range(256)}},
                "b": {"type": "object", "properties": {f"p{i}": leaf for i in range(130)}},
            },
        }
    )
    assert graph_counts(document)[0] == 99_986
    with pytest.raises(OutputConfigError, match="config_budget"):
        load_output_config(document)


def test_shared_children_are_charged_per_occurrence_and_cycles_refused() -> None:
    shared = {"type": "string"}
    document = config({"type": "object", "properties": {"a": shared, "b": shared}})
    nodes = graph_counts(document)[0]
    loaded = load_output_config(document, limits=OutputConfigLimits(max_nodes=nodes))
    with pytest.raises(OutputConfigError, match="config_budget"):
        load_output_config(document, limits=OutputConfigLimits(max_nodes=nodes - 1))
    shared["type"] = "null"
    assert loaded.validate({"a": "x", "b": "y"}).valid
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(OutputConfigError, match="config_cycle"):
        load_output_config(config(rules=cyclic))


class Hostile:
    def __repr__(self) -> str:
        raise AssertionError("repr hook called")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("equality hook called")

    def __hash__(self) -> int:
        return hash("kind")


@pytest.mark.parametrize(
    "document",
    [
        Hostile(),
        config(version=Hostile()),
        config({Hostile(): "string"}),
        config(rules=[Hostile()]),
    ],
)
def test_hostile_graph_nodes_are_refused_without_hooks(document: Any) -> None:
    with pytest.raises(OutputConfigError):
        load_output_config(document)


def test_builtin_subclasses_and_nonfinite_values_are_not_coerced() -> None:
    class ForeignDict(dict[str, Any]):
        def items(self) -> Any:
            raise AssertionError("mapping hook called")

    class ForeignString(str):
        def __eq__(self, other: object) -> bool:
            raise AssertionError("string comparison called")

    for value in (
        ForeignDict(config()),
        config(kind=ForeignString("payload-output-config")),
        config(version=float("nan")),
        config(version=float("inf")),
        config(version=10**1000),
        config(kind="\ud800"),
    ):
        with pytest.raises(OutputConfigError):
            load_output_config(value)


def test_export_refuses_custom_callbacks_and_subclasses_without_inspection() -> None:
    class Validator:
        armed = False

        def __getattribute__(self, name: str) -> Any:
            if name != "armed" and self.armed:
                raise AssertionError("foreign validator inspected")
            return object.__getattribute__(self, name)

        def check(self, value: Any, context: RuleContext) -> RuleResult:
            raise AssertionError("validator called")

    validator = Validator()
    pipeline = ValidationPipeline(OutputSchema("string"), (RuleBinding("custom", (), validator),))
    validator.armed = True
    for export in (export_output_config, output_config_digest):
        with pytest.raises(OutputConfigError, match="config_validator"):
            export(pipeline)

    class DerivedTrim(TrimmedString):
        pass

    class DerivedChoices(StringChoices):
        pass

    class DerivedPipeline(ValidationPipeline):
        def __getattribute__(self, name: str) -> Any:
            if name == "limits":
                raise AssertionError("subclass inspected")
            return super().__getattribute__(name)

    for foreign in (DerivedTrim(), DerivedChoices(("x",))):
        with pytest.raises(OutputConfigError, match="config_validator"):
            export_output_config(
                ValidationPipeline(OutputSchema("string"), (RuleBinding("custom", (), foreign),))
            )
    derived = object.__new__(DerivedPipeline)
    with pytest.raises(OutputConfigError, match="config_type"):
        export_output_config(derived)
    with pytest.raises(OutputConfigError, match="config_type"):
        export_output_config(Hostile())  # type: ignore[arg-type]


def test_compilation_export_and_identity_do_not_execute_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("callback executed during configuration")

    monkeypatch.setattr(TrimmedString, "check", fail)
    monkeypatch.setattr(StringChoices, "check", fail)
    pipeline = load_output_config(
        config(rules=[rule(), rule({"kind": "string_choices", "choices": ["x"]}, id="choices")])
    )
    assert export_output_config(pipeline)
    assert output_config_digest(pipeline)


def test_limit_object_types_and_controls_are_not_flattened(monkeypatch: pytest.MonkeyPatch) -> None:
    for function, argument in (
        (load_output_config, config()),
        (load_output_config_json_bytes, b"{}"),
        (export_output_config, ValidationPipeline(OutputSchema("string"))),
        (output_config_digest, ValidationPipeline(OutputSchema("string"))),
    ):
        with pytest.raises(ValueError, match="limits must be"):
            function(argument, limits={})  # type: ignore[operator]
    import payload_palette.output_config as implementation

    interrupt = KeyboardInterrupt("private control")

    def stop(*args: Any, **kwargs: Any) -> Any:
        raise interrupt

    monkeypatch.setattr(implementation, "load_output_schema", stop)
    with pytest.raises(KeyboardInterrupt) as caught:
        load_output_config(config())
    assert caught.value is interrupt
