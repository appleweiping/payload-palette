import inspect
import types
from dataclasses import dataclass, replace
from typing import Annotated

import pytest

import payload_palette.call_validation as module
from payload_palette import (
    CallAdapter,
    CallLimits,
    FieldConstraints,
    OutputContractError,
    OutputLimits,
    PayloadValidationError,
    SchemaDefinitionError,
    SchemaDefinitionLimits,
)


@pytest.mark.parametrize(
    "name,maximum",
    [
        ("max_parameters", 256),
        ("max_arguments", 100_000),
        ("max_keyword_characters", 1_000_000),
        ("max_steps", 1_000_000),
    ],
)
@pytest.mark.parametrize("bad", [False, True, 0, -1, 1.0, float("nan"), "1", None, 1 << 10_000])
def test_call_limits_reject_bad_values_without_large_integer_formatting(name, maximum, bad):
    with pytest.raises(ValueError):
        CallLimits(**{name: bad})
    assert getattr(CallLimits(**{name: 1}), name) == 1
    assert getattr(CallLimits(**{name: maximum}), name) == maximum
    with pytest.raises(ValueError):
        CallLimits(**{name: maximum + 1})


@pytest.mark.parametrize(
    "options",
    [
        {"limits": None},
        {"definition_limits": {}},
        {"call_limits": {}},
        {"validate_return": 1},
        {"validate_return": None},
    ],
)
def test_options_need_the_exact_declared_types(options):
    def target():
        pass

    with pytest.raises(ValueError):
        CallAdapter(target, annotations={}, **options)


def test_parameter_count_limit_and_hard_ceiling_before_annotation_metadata():
    def template():
        return None

    for count in (1, 64, 256):
        names = tuple(f"p{n}" for n in range(count))
        code = template.__code__.replace(
            co_argcount=count, co_posonlyargcount=count, co_varnames=names, co_nlocals=count
        )
        target = types.FunctionType(code, {})
        hints = dict.fromkeys(names, int)
        adapter = CallAdapter(
            target, annotations=hints, call_limits=CallLimits(max_parameters=count)
        )
        assert adapter.call(*([1] * count)) is None
        if count > 1:
            with pytest.raises(SchemaDefinitionError, match="parameter count"):
                CallAdapter(
                    target, annotations=hints, call_limits=CallLimits(max_parameters=count - 1)
                )


def test_raw_and_completed_argument_limits_are_both_enforced_before_target():
    entries = []

    def target(a=1, *values, b=2, **extras):
        entries.append(1)
        return a, values, b, extras

    hints = {"a": int, "values": int, "b": int, "extras": int}
    adapter = CallAdapter(target, annotations=hints, call_limits=CallLimits(max_arguments=3))
    assert adapter.call(1, 2) == (1, (2,), 2, {})
    assert entries == [1]
    with pytest.raises(OutputContractError, match="completed"):
        adapter.call(1, 2, 3)
    with pytest.raises(OutputContractError, match="raw"):
        adapter.call(1, 2, 3, 4)
    with pytest.raises(OutputContractError, match="completed"):
        CallAdapter(target, annotations=hints, call_limits=CallLimits(max_arguments=1)).call()
    assert entries == [1]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("variadic", [False, True])
def test_keyword_subclass_admission_precedes_length_hooks_and_target(asynchronous, variadic):
    import asyncio

    events = []

    class Name(str):
        def __len__(self):
            events.append("length-hook")
            raise RuntimeError("PRIVATE keyword")

    def fixed(value):
        events.append("target")
        return value

    def extra(**values):
        events.append("target")
        return values

    async def async_fixed(value):
        return fixed(value)

    async def async_extra(**values):
        return extra(**values)

    target = (
        (async_extra if variadic else async_fixed)
        if asynchronous
        else (extra if variadic else fixed)
    )
    adapter = CallAdapter(target, annotations={"values" if variadic else "value": int})
    with pytest.raises(OutputContractError, match="exact built-in strings"):
        if asynchronous:
            asyncio.run(adapter.call_async(**{Name("value"): 1}))
        else:
            adapter.call(**{Name("value"): 1})
    assert events == []


def test_keyword_names_aggregate_bound_unicode_and_absolute_ceiling():
    def target(**extras):
        return extras

    adapter = CallAdapter(
        target, annotations={"extras": int}, call_limits=CallLimits(max_keyword_characters=3)
    )
    assert adapter.call(**{"文": 1, "😀x": 2}) == {"文": 1, "😀x": 2}
    for kwargs in ({"ab": 1, "cd": 2}, {"\ud800": 1}):
        with pytest.raises(OutputContractError):
            adapter.call(**kwargs)
    unbounded_name = CallAdapter(target, annotations={"extras": int})
    assert unbounded_name.call(**{"x" * 256: 1}) == {"x" * 256: 1}
    with pytest.raises(OutputContractError):
        unbounded_name.call(**{"x" * 257: 1})


@pytest.mark.parametrize("field,at", [("max_nodes", 4), ("max_characters", 3)])
def test_argument_and_return_graphs_share_size_budget(field, at):
    entries = []

    def target(a, b):
        entries.append(1)
        return a

    # nodes: argument record + two ints + returned int = 4.
    # characters: argument keys a/b (2) + return value 'x' would add data.
    if field == "max_characters":

        def target(v):
            entries.append(1)
            return v

        hints = {"v": str, "return": str}
        arguments = ("x",)
    else:
        hints = {"a": int, "b": int, "return": int}
        arguments = (1, 2)
    assert (
        CallAdapter(
            target, annotations=hints, validate_return=True, limits=OutputLimits(**{field: at})
        ).call(*arguments)
        == arguments[0]
    )
    with pytest.raises(OutputContractError):
        CallAdapter(
            target, annotations=hints, validate_return=True, limits=OutputLimits(**{field: at - 1})
        ).call(*arguments)
    assert entries == [1, 1]


def test_multiple_arguments_cannot_reset_projection_budget_or_enter_target():
    def target(a, b):
        pytest.fail("oversized complete input entered target")

    adapter = CallAdapter(
        target, annotations={"a": list[int], "b": list[int]}, limits=OutputLimits(max_nodes=4)
    )
    with pytest.raises(OutputContractError):
        adapter.call([1], [2])  # each child individually fits; record totals five nodes.


def test_default_projection_uses_one_aggregate_budget_at_compilation():
    def target(a=1, b=2):
        return a + b

    assert (
        CallAdapter(
            target, annotations={"a": int, "b": int}, limits=OutputLimits(max_nodes=3)
        ).call()
        == 3
    )
    with pytest.raises(OutputContractError):
        CallAdapter(target, annotations={"a": int, "b": int}, limits=OutputLimits(max_nodes=2))


def test_definition_forest_is_shared_not_one_budget_per_parameter():
    def target(a, b):
        return a, b

    # Forest + argument record + two scalar leaves = four definition nodes.
    assert CallAdapter(
        target,
        annotations={"a": int, "b": int},
        definition_limits=SchemaDefinitionLimits(max_nodes=4),
    ).call(1, 2) == (1, 2)
    with pytest.raises(SchemaDefinitionError):
        CallAdapter(
            target,
            annotations={"a": int, "b": int},
            definition_limits=SchemaDefinitionLimits(max_nodes=3),
        )
    with pytest.raises(SchemaDefinitionError):
        CallAdapter(
            target,
            annotations={"a": int, "b": int, "return": int},
            definition_limits=SchemaDefinitionLimits(max_nodes=4),
        )


def test_exact_schema_and_native_work_counter_boundary_spans_return():
    entries = []

    def target(v):
        entries.append(1)
        return v

    hints = {"v": int, "return": int}
    assert (
        CallAdapter(
            target,
            annotations=hints,
            validate_return=True,
            limits=OutputLimits(max_schema_steps=3),
            call_limits=CallLimits(max_steps=10),
        ).call(1)
        == 1
    )
    with pytest.raises(OutputContractError):
        CallAdapter(
            target, annotations=hints, validate_return=True, limits=OutputLimits(max_schema_steps=2)
        ).call(1)
    with pytest.raises(OutputContractError):
        CallAdapter(
            target, annotations=hints, validate_return=True, call_limits=CallLimits(max_steps=9)
        ).call(1)
    assert entries == [1, 1, 1]


def test_invalid_native_graphs_and_resource_failure_are_not_union_mismatches():
    def target(v):
        return v

    for value in (float("nan"), float("inf"), 1 << 850, "\ud800"):
        with pytest.raises((PayloadValidationError, OutputContractError)):
            CallAdapter(target, annotations={"v": int | float | str}).call(value)
    recursive = []
    recursive.append(recursive)
    with pytest.raises(OutputContractError, match="cyclic"):
        CallAdapter(target, annotations={"v": list[list[int]]}).call(recursive)
    with pytest.raises(OutputContractError):
        CallAdapter(
            target, annotations={"v": int | float | str}, limits=OutputLimits(max_schema_steps=1)
        ).call(1)


def test_malformed_function_defaults_and_keyword_defaults_are_rejected():
    def target(a=1, *, b=2):
        return a + b

    target.__defaults__ = (1, 2)
    with pytest.raises(SchemaDefinitionError):
        CallAdapter(target, annotations={"a": int, "b": int})
    target.__defaults__ = (1,)
    target.__kwdefaults__ = {"a": 2}
    with pytest.raises(SchemaDefinitionError):
        CallAdapter(target, annotations={"a": int, "b": int})


def test_foreign_annotation_object_is_not_rendered_in_diagnostics():
    class Hostile:
        def __repr__(self):
            pytest.fail("annotation repr executed")

    def target(v):
        return v

    with pytest.raises(SchemaDefinitionError):
        CallAdapter(target, annotations={"v": Hostile()})


def test_return_projection_allocation_failure_propagates_after_real_target(monkeypatch):
    entries = []

    def target(v):
        entries.append(1)
        return v

    adapter = CallAdapter(target, annotations={"v": int, "return": int}, validate_return=True)
    original = module._project
    failure = MemoryError("allocation")

    def project(plan, value, work, path=(), *args, **kwargs):
        if path == ("return",):
            raise failure
        return original(plan, value, work, path, *args, **kwargs)

    monkeypatch.setattr(module, "_project", project)
    with pytest.raises(MemoryError) as caught:
        adapter.call(1)
    assert caught.value is failure and entries == [1]


def test_code_replaced_during_validation_is_refused_before_entry(monkeypatch):
    def target(v):
        return v

    def replacement(v):
        pytest.fail("replacement was called")

    adapter = CallAdapter(target, annotations={"v": int})
    original = module._project

    def project(*args, **kwargs):
        value = original(*args, **kwargs)
        target.__code__ = replacement.__code__
        return value

    monkeypatch.setattr(module, "_project", project)
    with pytest.raises(OutputContractError, match="code changed during"):
        adapter.call(1)


def test_invalid_native_code_parameter_name_is_refused():
    def template(v):
        return v

    code = template.__code__.replace(co_varnames=("not an identifier",))
    target = types.FunctionType(code, {})
    with pytest.raises(SchemaDefinitionError, match="parameter name"):
        CallAdapter(target, annotations={"not an identifier": int})


@pytest.mark.parametrize("names", [("for",), ("duplicate", "duplicate")])
def test_forged_keyword_or_duplicate_parameter_names_are_structured_definition_errors(names):
    def template():
        return None

    code = template.__code__.replace(
        co_argcount=len(names), co_nlocals=len(names), co_varnames=names
    )
    with pytest.raises(SchemaDefinitionError):
        CallAdapter(types.FunctionType(code, {}), annotations=dict.fromkeys(names, int))


def test_native_dataclass_subclass_and_malformed_scalar_are_rejected():
    @dataclass
    class Base:
        value: int

    @dataclass
    class Child(Base):
        pass

    def target(v):
        return v

    with pytest.raises(PayloadValidationError):
        CallAdapter(target, annotations={"v": Base}).call(Child(1))
    from ipaddress import IPv4Address

    address = IPv4Address("192.0.2.1")
    address._ip = -1
    with pytest.raises(PayloadValidationError):
        CallAdapter(target, annotations={"v": IPv4Address}).call(address)


def test_native_plan_has_no_default_factory_or_constructor_admission_callbacks():
    @dataclass
    class Box:
        value: int

    box = Box(1)
    Box.__init__ = None

    def target(v=box):
        return v

    assert (
        CallAdapter(target, annotations={"v": Box}, limits=OutputLimits(max_invocations=1)).call()
        is box
    )


def test_limit_configuration_is_immutable_and_constraints_do_not_coerce():
    def target(v):
        return v

    hints = {"v": Annotated[tuple[int, ...], FieldConstraints(min_length=1, max_length=2)]}
    adapter = CallAdapter(target, annotations=hints)
    assert adapter.call((1, 2)) == (1, 2)
    with pytest.raises(PayloadValidationError):
        adapter.call((1, 2, 3))
    assert replace(adapter.call_limits, max_arguments=1).max_arguments == 1
    assert (
        inspect.signature(adapter.call).parameters["kwargs"].kind is inspect.Parameter.VAR_KEYWORD
    )
