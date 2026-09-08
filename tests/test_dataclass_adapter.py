"""Real typed results and observable callback ownership, not dict-only wrappers."""

import asyncio
import inspect
import json
from dataclasses import InitVar, dataclass, field, make_dataclass
from typing import Annotated, ClassVar, Generic, Literal, NotRequired, TypedDict, TypeVar

import pytest

import payload_palette as api
from payload_palette import FieldConstraints, OutputContractError, PayloadValidationError
from payload_palette.schema_io import SchemaDefinitionError


@dataclass(frozen=True, slots=True)
class Detection:
    label: Literal["car", "person"]
    confidence: Annotated[float, FieldConstraints(minimum=0, maximum=1)]


@dataclass(kw_only=True)
class Response:
    source: str
    items: list[Detection]
    note: str | None = None
    tags: list[str] = field(default_factory=list)
    protocol: ClassVar[str] = "local"


def adapter(model, **kwargs):
    return api.DataclassAdapter(model, **kwargs)


def test_constructs_exact_nested_classes_and_roundtrips_with_defaults():
    compiled = adapter(Response)
    source = {"source": "camera", "items": [{"label": "car", "confidence": 0.8}]}
    model = compiled.validate_python(source)
    assert type(model) is Response and type(model.items[0]) is Detection
    assert model.note is None and model.tags == []
    assert set(compiled.input_schema.required) == {"source", "items"}
    assert set(compiled.output_schema.required) == {"source", "items", "note", "tags"}
    assert "protocol" not in compiled.output_schema.properties
    wire = compiled.dump_python(model)
    assert wire == {**source, "note": None, "tags": []}
    assert compiled.validate_json_bytes(compiled.dump_json(model)) == model
    model.tags.append("new")
    assert "tags" not in source and wire["tags"] == []
    source["items"][0]["label"] = "person"
    assert model.items[0].label == "car"


def test_no_factory_or_constructor_on_invalid_supplied_field_or_ambiguous_union():
    calls = []

    def default():
        calls.append("factory")
        return 1

    @dataclass
    class First:
        value: int
        extra: int = field(default_factory=default)

        def __post_init__(self):
            calls.append("constructor")

    Second = make_dataclass("Second", [("value", int), ("extra", int, field(default=1))])
    Holder = make_dataclass("Holder", [("choice", First | Second), ("count", int)])
    compiled = adapter(Holder)
    assert calls == []
    with pytest.raises(PayloadValidationError):
        compiled.validate_python({"choice": {"value": 1}, "count": "bad"})
    with pytest.raises(PayloadValidationError, match="dataclass_union_ambiguous"):
        compiled.validate_python({"choice": {"value": 1}, "count": 1})
    assert calls == []


def test_factory_native_nested_models_are_projected_and_reconstructed_without_aliases():
    retained, calls = [], []

    @dataclass
    class Child:
        values: list[int]

        def __post_init__(self):
            calls.append("child")

    def produce():
        item = Child([1])
        retained.append(item)
        return item

    Parent = make_dataclass("Parent", [("child", Child, field(default_factory=produce))])
    compiled = adapter(Parent)
    assert not calls
    first = compiled.validate_python({})
    second = compiled.validate_python({})
    assert calls == ["child", "child", "child", "child"]
    assert first.child is not retained[0] and second.child is not retained[1]
    retained[0].values.append(2)
    first.child.values.append(3)
    assert second.child.values == [1]
    assert compiled.dump_python(first) == {"child": {"values": [1, 3]}}
    assert len(calls) == 4  # Dump never constructs objects or invokes default factories.


def test_default_literals_are_snapshotted_without_skipping_real_constructors():
    calls = []

    @dataclass(frozen=True)
    class Child:
        values: list[int]

        def __post_init__(self):
            calls.append("constructed")

    literal = Child([1])
    Parent = make_dataclass("Parent", [("child", Child, field(default=literal))])
    compiled = adapter(Parent)
    assert calls == ["constructed"]
    literal.values.append(99)
    model = compiled.validate_python({})
    assert model.child.values == [1] and model.child is not literal
    assert calls == ["constructed", "constructed"]


def test_invalid_defaults_and_post_init_mutation_never_become_accepted_models():
    @dataclass
    class Corrupt:
        value: int

        def __post_init__(self):
            self.value = "secret invalid result"

    with pytest.raises(PayloadValidationError):
        adapter(Corrupt).validate_python({"value": 1})
    Invalid = make_dataclass("Invalid", [("value", int, field(default="bad"))])
    with pytest.raises(PayloadValidationError):
        adapter(Invalid)
    Factory = make_dataclass("Factory", [("value", int, field(default_factory=lambda: "bad"))])
    with pytest.raises(PayloadValidationError):
        adapter(Factory).validate_python({})


def test_dump_revalidates_mutated_models_and_does_not_accept_shape_only_impostors():
    compiled = adapter(Response)
    model = Response(source="x", items=[])
    model.tags.append(1)
    with pytest.raises(PayloadValidationError):
        compiled.dump_json(model)
    with pytest.raises(PayloadValidationError):
        compiled.dump_python({"source": "x", "items": [], "note": None, "tags": []})
    model.tags.clear()
    assert json.loads(compiled.dump_json(model))["tags"] == []


def test_callback_failures_are_sanitized_and_control_exceptions_propagate():
    def failure():
        raise RuntimeError("private provider response must not enter issues")

    Model = make_dataclass("Model", [("value", int, field(default_factory=failure))])
    with pytest.raises(PayloadValidationError) as caught:
        adapter(Model).validate_python({})
    assert "private provider" not in str(caught.value)
    assert caught.value.issues[0].path == '$["value"]'

    def interrupt():
        raise KeyboardInterrupt("stop")

    Interrupted = make_dataclass("Interrupted", [("value", int, field(default_factory=interrupt))])
    with pytest.raises(KeyboardInterrupt, match="stop"):
        adapter(Interrupted).validate_python({})


def test_shared_callback_and_materialization_budgets_fail_closed():
    compiled = adapter(Response, construction=api.DataclassLimits(max_callbacks=1))
    with pytest.raises(OutputContractError, match="callback"):
        compiled.validate_python({"source": "x", "items": [{"label": "car", "confidence": 1}]})
    with pytest.raises(OutputContractError, match="materialization"):
        compiled = adapter(Response, construction=api.DataclassLimits(max_steps=1))
        compiled.validate_python({"source": "x", "items": []})


def test_inherited_keyword_only_slots_and_nullable_required_fields():
    @dataclass(frozen=True, slots=True, kw_only=True)
    class Base:
        identifier: int

    @dataclass(frozen=True, slots=True, kw_only=True)
    class Child(Base):
        note: str | None
        count: int = 3

    compiled = adapter(Child)
    with pytest.raises(PayloadValidationError):
        compiled.validate_python({"identifier": 1})
    result = compiled.validate_python({"identifier": 1, "note": None})
    assert result == Child(identifier=1, note=None)
    assert compiled.dump_python(result) == {"identifier": 1, "note": None, "count": 3}
    assert not hasattr(result, "__dict__")


def test_typed_records_maps_and_annotated_containers_construct_nested_values():
    class Record(TypedDict):
        first: Detection
        optional: NotRequired[Detection]

    Model = make_dataclass(
        "Model",
        [
            ("record", Record),
            ("mapping", dict[str, Detection]),
            ("items", Annotated[list[Detection], FieldConstraints(max_length=2)]),
        ],
    )
    compiled = adapter(Model)
    sample = {"label": "person", "confidence": 1}
    wire = {"record": {"first": sample}, "mapping": {"id": sample}, "items": [sample]}
    result = compiled.validate_python(wire)
    assert type(result.record["first"]) is Detection
    assert type(result.mapping["id"]) is Detection
    assert type(result.items[0]) is Detection
    assert compiled.dump_python(result) == wire
    assert "optional" not in result.record
    assert result.items[0] is not result.mapping["id"]
    wire["items"] *= 3
    with pytest.raises(PayloadValidationError):
        compiled.validate_python(wire)


def test_discriminated_structural_unions_and_json_only_overlap():
    A = make_dataclass("A", [("kind", Literal["a"]), ("value", int)])
    B = make_dataclass("B", [("kind", Literal["b"]), ("text", str)])
    Model = make_dataclass("Model", [("choice", A | B | None), ("number", int | float)])
    compiled = adapter(Model)
    for source, expected in [
        (None, type(None)),
        ({"kind": "a", "value": 2}, A),
        ({"kind": "b", "text": "ok"}, B),
    ]:
        result = compiled.validate_python({"choice": source, "number": 1})
        assert type(result.choice) is expected and type(result.number) is int
        assert compiled.dump_python(result) == {"choice": source, "number": 1}
    with pytest.raises(PayloadValidationError):
        compiled.dump_python(Model(object(), 1))


@pytest.mark.parametrize("value", [True, 1.0, "1", None, [1], {"x": 1}])
def test_integer_fields_reject_coercion_on_input_and_dump(value):
    Model = make_dataclass("Model", [("value", int)])
    compiled = adapter(Model)
    with pytest.raises(PayloadValidationError):
        compiled.validate_python({"value": value})
    with pytest.raises(PayloadValidationError):
        compiled.dump_python(Model(value))


def test_all_supplied_fields_and_nested_ambiguity_preflight_before_callbacks():
    calls = []
    A = make_dataclass("A", [("x", int)])
    B = make_dataclass("B", [("x", int)])
    Model = make_dataclass(
        "Model",
        [("choices", list[A | B]), ("value", int, field(default_factory=lambda: calls.append(1)))],
    )
    compiled = adapter(Model)
    with pytest.raises(PayloadValidationError, match="ambiguous"):
        compiled.validate_python({"choices": [{"x": 1}]})
    assert calls == []
    with pytest.raises(PayloadValidationError):
        compiled.validate_python({"choices": [], "extra": "bad"})
    assert calls == []


def test_all_factory_defaults_prepared_before_adapter_constructs_any_child():
    calls = []

    @dataclass
    class Child:
        value: int

        def __post_init__(self):
            calls.append("child")

    Model = make_dataclass(
        "Model",
        [("child", Child), ("bad", int, field(default_factory=lambda: "bad"))],
    )
    with pytest.raises(PayloadValidationError):
        adapter(Model).validate_python({"child": {"value": 1}})
    assert calls == []


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit, asyncio.CancelledError])
@pytest.mark.parametrize("stage", ["factory", "constructor"])
def test_control_exception_identity_is_not_sanitized(control, stage):
    expected = control("caller control")

    def fail(*args, **kwargs):
        raise expected

    if stage == "factory":
        Model = make_dataclass("Model", [("value", int, field(default_factory=fail))])
        source = {}
    else:
        Model = make_dataclass("Model", [("value", int)], namespace={"__post_init__": fail})
        source = {"value": 1}
    with pytest.raises(control) as caught:
        adapter(Model).validate_python(source)
    assert caught.value is expected


def test_constructor_error_privacy_and_incorrect_return_types():
    def fail(self):
        raise RuntimeError("private constructor text")

    Model = make_dataclass("Model", [("value", int)], namespace={"__post_init__": fail})
    with pytest.raises(PayloadValidationError) as caught:
        adapter(Model).validate_python({"value": 1})
    assert "private constructor" not in str(caught.value)
    assert caught.value.issues[0].code == "dataclass_constructor"

    class Meta(type):
        def __call__(cls, **kwargs):
            return kwargs

    @dataclass
    class Wrong(metaclass=Meta):
        value: int

    with pytest.raises(PayloadValidationError, match="dataclass_constructor"):
        adapter(Wrong).validate_python({"value": 1})


def test_fresh_native_coroutine_return_is_closed_without_executing_body():
    made, entered = [], []

    async def produce():
        entered.append(1)
        return 1

    def factory():
        result = produce()
        made.append(result)
        return result

    Model = make_dataclass("Model", [("value", int, field(default_factory=factory))])
    with pytest.raises(PayloadValidationError):
        adapter(Model).validate_python({})
    assert entered == []
    with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
        made[0].send(None)


def test_borrowed_started_coroutine_task_future_and_custom_awaitable_are_untouched():
    async def scenario():
        entered, closed = [], []

        async def wait():
            entered.append(1)
            try:
                await asyncio.sleep(0)
            finally:
                closed.append(1)

        started = wait()
        started.send(None)
        task = asyncio.create_task(wait())
        future = asyncio.get_running_loop().create_future()

        class Awaitable:
            def __await__(self):
                raise AssertionError("borrowed awaitable advanced")

            def close(self):
                raise AssertionError("borrowed awaitable closed")

        try:
            for value in (started, task, future, Awaitable()):
                Model = make_dataclass(
                    "Model", [("value", int, field(default_factory=lambda v=value: v))]
                )
                with pytest.raises(PayloadValidationError):
                    adapter(Model).validate_python({})
            assert inspect.getcoroutinestate(started) == inspect.CORO_SUSPENDED
            assert not task.done() and not future.done()
            assert entered == [1] and closed == []
        finally:
            started.close()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            future.cancel()

    asyncio.run(scenario())


@pytest.mark.parametrize("hook", ["__init__", "__new__", "__post_init__"])
def test_known_async_constructors_rejected_at_admission(hook):
    async def callback(*args, **kwargs):
        return None

    Model = make_dataclass("Model", [("value", int)], namespace={hook: callback})
    with pytest.raises(SchemaDefinitionError, match="dataclass_constructor"):
        adapter(Model)


def test_async_factory_callable_and_generator_rejected_without_invocation():
    async def factory():
        return 1

    async def generator():
        yield 1

    class AsyncCallable:
        async def __call__(self):
            return 1

    for callback in (factory, generator, AsyncCallable(), None, 1):
        Model = make_dataclass("Model", [("value", int, field(default_factory=callback))])
        with pytest.raises(SchemaDefinitionError, match="dataclass_factory"):
            adapter(Model)


def test_custom_descriptors_initvar_init_false_metadata_and_generic_rejected():
    class Descriptor:
        def __get__(self, instance, owner):
            return 1

        def __set__(self, instance, value):
            pass

    models = [
        make_dataclass("Model", [("value", int, Descriptor())]),
        make_dataclass("Model", [("value", InitVar[int])]),
        make_dataclass("Model", [("value", int, field(default=1, init=False))]),
        make_dataclass("Model", [("value", int, field(metadata={"hint": "not supported"}))]),
        make_dataclass("Model", [("value", int)], init=False),
        make_dataclass("Model", [("value", "Unresolved")]),
    ]
    parameter = TypeVar("parameter")

    @dataclass
    class GenericModel(Generic[parameter]):
        value: parameter

    models.extend([GenericModel, GenericModel[int]])
    for model in models:
        with pytest.raises(SchemaDefinitionError):
            adapter(model)


def test_recursive_metadata_and_invalid_constructor_keyword_contract_rejected():
    Recursive = make_dataclass("Recursive", [("value", int)])
    Recursive.__dataclass_fields__["value"].type = Recursive
    with pytest.raises(SchemaDefinitionError, match="cycle"):
        adapter(Recursive)
    Model = make_dataclass("Model", [("value", int)], namespace={"__init__": lambda self: None})
    with pytest.raises(SchemaDefinitionError, match="constructor"):
        adapter(Model)


@pytest.mark.parametrize("root", [int, dict, {}, Detection("car", 0.5), list[Detection]])
def test_root_requires_concrete_dataclass_type(root):
    with pytest.raises(SchemaDefinitionError):
        adapter(root)


@pytest.mark.parametrize("name", ["limits", "definition_limits", "construction", "serialization"])
def test_config_exact_types(name):
    with pytest.raises(ValueError):
        adapter(Detection, **{name: {}})


@pytest.mark.parametrize("name", ["max_steps", "max_callbacks", "max_default_bytes"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.0, 10**100])
def test_dataclass_limit_admission(name, value):
    with pytest.raises(ValueError):
        api.DataclassLimits(**{name: value})


def test_empty_model_literal_bytes_and_shared_schema_budget():
    Empty = make_dataclass("Empty", [])
    assert adapter(Empty).dump_python(adapter(Empty).validate_python({})) == {}
    Model = make_dataclass("Model", [("a", str, field(default="xy")), ("b", int, field(default=1))])
    with pytest.raises(OutputContractError, match="byte limit"):
        adapter(Model, construction=api.DataclassLimits(max_default_bytes=4))
    compiled = adapter(Detection, limits=api.OutputLimits(max_schema_steps=1))
    with pytest.raises(OutputContractError, match="schema"):
        compiled.validate_python({"label": "car", "confidence": 0.5})


def test_cycles_oversized_defaults_and_mutated_fields_fail_closed():
    Model = make_dataclass("Model", [("values", list[list[int]])])
    compiled = adapter(Model)
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(OutputContractError, match="cyclic"):
        compiled.dump_python(Model(cyclic))
    small = adapter(Model, limits=api.OutputLimits(max_nodes=3))
    with pytest.raises(OutputContractError, match="node"):
        small.dump_python(Model([[1, 2, 3]]))
    Short = make_dataclass("Short", [("text", str, field(default="long"))])
    with pytest.raises(OutputContractError, match="character"):
        adapter(Short, limits=api.OutputLimits(max_characters=3))
    result = Model([])
    del result.values
    with pytest.raises(PayloadValidationError, match="attribute"):
        compiled.dump_python(result)


def test_strict_bytes_ingress_and_output_byte_limit():
    compiled = adapter(Detection)
    for raw in (
        b'{"label":"car","label":"car","confidence":1}',
        b'{"label":"car","confidence":NaN}',
    ):
        with pytest.raises(ValueError):
            compiled.validate_json_bytes(raw)
    bounded = adapter(Detection, serialization=api.JSONSerializationOptions(max_output_bytes=5))
    with pytest.raises(OutputContractError, match="byte limit"):
        bounded.dump_json(Detection("car", 1))


def test_dump_also_requires_unambiguous_input_roundtrip_shape():
    A = make_dataclass("A", [("value", int, field(default=1))])
    B = make_dataclass("B", [("value", int, field(default=2)), ("extra", int, field(default=3))])
    Model = make_dataclass("Model", [("choice", A | B)])
    compiled = adapter(Model)
    # A's complete output matches B's *input* too, because B.extra may be omitted.
    # Knowing the runtime A type does not make its JSON reconstruct unambiguously.
    with pytest.raises(PayloadValidationError, match="ambiguous"):
        compiled.dump_python(Model(A()))
    assert compiled.dump_python(Model(B())) == {"choice": {"value": 2, "extra": 3}}


def test_contract_is_final_graph_validation_not_parent_callback_input_validation():
    calls = []

    @dataclass
    class Child:
        value: int

        def __post_init__(self):
            self.value = "temporary"

    @dataclass
    class Parent:
        child: Child

        def __post_init__(self):
            calls.append(self.child.value)
            self.child.value = 2

    result = adapter(Parent).validate_python({"child": {"value": 1}})
    assert calls == ["temporary"] and result.child.value == 2


def test_coroutine_return_from_metaclass_is_owned_but_no_body_is_executed():
    made, calls = [], []

    async def response():
        calls.append(1)

    class Meta(type):
        def __call__(cls, **kwargs):
            value = response()
            made.append(value)
            return value

    @dataclass
    class Model(metaclass=Meta):
        value: int

    with pytest.raises(PayloadValidationError, match="constructor"):
        adapter(Model).validate_python({"value": 1})
    assert calls == []
    with pytest.raises(RuntimeError, match="cannot reuse already awaited coroutine"):
        made[0].send(None)


def test_async_metaclass_and_constructor_mutation_are_rejected():
    class AsyncMeta(type):
        @staticmethod
        async def __call__(**kwargs):
            return kwargs

    @dataclass
    class AsyncModel(metaclass=AsyncMeta):
        value: int

    with pytest.raises(SchemaDefinitionError, match="constructor"):
        adapter(AsyncModel)
    Model = make_dataclass("Model", [("value", int)])
    compiled = adapter(Model)

    async def changed(self, value):
        raise AssertionError("async constructor must not run")

    Model.__init__ = changed
    with pytest.raises(PayloadValidationError, match="constructor"):
        compiled.validate_python({"value": 1})


def test_malformed_dataclass_metadata_is_rejected_not_silently_ignored():
    for mutation in ("wrong_field", "wrong_name", "long_name", "both_defaults", "not_dict"):
        Model = make_dataclass("Model", [("value", int)])
        members = Model.__dataclass_fields__
        if mutation == "wrong_field":
            members["value"] = object()
        elif mutation == "wrong_name":
            members["value"].name = "other"
        elif mutation == "long_name":
            item = members.pop("value")
            item.name = "v" * 257
            members[item.name] = item
        elif mutation == "both_defaults":
            members["value"].default = 1
            members["value"].default_factory = lambda: 1
        else:
            Model.__dataclass_fields__ = []
        with pytest.raises(SchemaDefinitionError):
            adapter(Model)
    Unsupported = make_dataclass("Unsupported", [("value", set[int])])
    with pytest.raises(SchemaDefinitionError):
        adapter(Unsupported)


@pytest.mark.parametrize("value", [(1,), {"x": 1}, "one"])
def test_dump_rejects_list_impostors(value):
    Model = make_dataclass("Model", [("values", list[int])])
    with pytest.raises(PayloadValidationError, match="built-in list"):
        adapter(Model).dump_python(Model(value))


def test_dump_record_keys_shape_and_depth_are_checked():
    class Record(TypedDict):
        value: int

    Model = make_dataclass("Model", [("record", Record)])
    compiled = adapter(Model)
    for record in ([1], {1: 1}, {"value": 1, "other": 2}):
        with pytest.raises(PayloadValidationError):
            compiled.dump_python(Model(record))
    bounded = adapter(Model, limits=api.OutputLimits(max_depth=1))
    with pytest.raises(OutputContractError, match="depth"):
        bounded.dump_python(Model({"value": 1}))


def test_compiled_default_total_cap_includes_every_literal():
    Model = make_dataclass("Model", [("a", int, field(default=1)), ("b", int, field(default=2))])
    with pytest.raises(OutputContractError, match="compiled default byte limit"):
        adapter(Model, construction=api.DataclassLimits(max_default_bytes=1))


def test_example_runs_offline_and_checks_the_real_typed_roundtrip(capsys):
    from examples.dataclass_adaptation import main

    main()
    assert json.loads(capsys.readouterr().out) == {
        "detections": [{"label": "bicycle", "score": 0.8}],
        "source": "offline-fixture",
        "tags": ["reviewed"],
    }
