"""Handwritten functions plus stdlib binding; no production binding helpers."""

import inspect
import itertools

import pytest

from payload_palette import CallAdapter, PayloadValidationError

ENTRIES = []


def empty():
    ENTRIES.append(1)
    return {}


def one(a):
    ENTRIES.append(1)
    return locals()


def two(a, b=2):
    ENTRIES.append(1)
    return locals()


def positional(a, /, b=2):
    ENTRIES.append(1)
    return locals()


def positional_defaults(a=1, b=2, /):
    ENTRIES.append(1)
    return locals()


def keyword_only(*, a, b=2):
    ENTRIES.append(1)
    return locals()


def variadic(*values):
    ENTRIES.append(1)
    return locals()


def keyword_variadic(**extras):
    ENTRIES.append(1)
    return locals()


def mixed(a, /, b=2, *values, flag=3, **extras):
    ENTRIES.append(1)
    return locals()


def required_keyword(a, *values, flag, **extras):
    ENTRIES.append(1)
    return locals()


def all_defaults(a=1, *values, b=2, **extras):
    ENTRIES.append(1)
    return locals()


def reserved_names(self, /, function=2, *, annotations=3, **limits):
    ENTRIES.append(1)
    return locals()


class Owner:
    def method(self, a, /, *, b=2, **extras):
        ENTRIES.append(1)
        return {"a": a, "b": b, "extras": extras}


FIXTURES = [
    empty,
    one,
    two,
    positional,
    positional_defaults,
    keyword_only,
    variadic,
    keyword_variadic,
    mixed,
    required_keyword,
    all_defaults,
    reserved_names,
    Owner().method,
]


@pytest.mark.parametrize("target", FIXTURES, ids=lambda target: target.__name__)
def test_original_binder_against_stdlib_and_real_function(target):
    signature = inspect.signature(target, eval_str=False, follow_wrapped=False)
    adapter = CallAdapter(target, annotations=dict.fromkeys(signature.parameters, int))
    assert [(p.name, p.kind, p.default) for p in adapter.signature.parameters.values()] == [
        (p.name, p.kind, p.default) for p in signature.parameters.values()
    ]
    names = (
        ("self", "function", "annotations", "z")
        if target is reserved_names
        else ("a", "b", "flag", "z")
    )
    vectors = accepted = rejected = inspection_disagreements = 0
    for size in range(5):
        args = tuple(range(10, 10 + size))
        for keyword_size in range(4):
            for keywords in itertools.permutations(names, keyword_size):
                kwargs = {name: 20 + index for index, name in enumerate(keywords)}
                vectors += 1
                ENTRIES.clear()
                try:
                    direct = target(*args, **kwargs)
                except TypeError:
                    actual_accepts = False
                    assert ENTRIES == []
                else:
                    actual_accepts = True
                    assert ENTRIES == [1]
                try:
                    bound = signature.bind(*args, **kwargs)
                except TypeError:
                    inspection_accepts = False
                else:
                    inspection_accepts = True
                if actual_accepts != inspection_accepts:
                    # Observed CPython 3.14.5 inspection bug: after an omitted
                    # defaulted positional-only slot, bind can accept a later
                    # positional-only keyword. The real call remains authority.
                    assert target is positional_defaults and not actual_accepts
                    assert inspection_accepts and any(name in kwargs for name in ("a", "b"))
                    inspection_disagreements += 1
                ENTRIES.clear()
                if not actual_accepts:
                    with pytest.raises(PayloadValidationError) as error:
                        adapter.call(*args, **kwargs)
                    assert error.value.issues[0].code == "call_bind"
                    assert ENTRIES == []
                    rejected += 1
                else:
                    bound.apply_defaults()
                    result = adapter.call(*args, **kwargs)
                    assert result == bound.arguments == direct
                    assert ENTRIES == [1]
                    assert target(*args, **kwargs) == result
                    for parameter in signature.parameters.values():
                        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
                            assert list(result[parameter.name]) == list(
                                bound.arguments[parameter.name]
                            )
                    accepted += 1
    assert vectors == accepted + rejected == 205
    print(
        {
            "fixture": target.__name__,
            "vectors": vectors,
            "accepted": accepted,
            "rejected": rejected,
            "inspection_disagreements": inspection_disagreements,
        }
    )


def test_handwritten_mixed_signature_anchor():
    adapter = CallAdapter(
        mixed, annotations={"a": int, "b": int, "values": int, "flag": int, "extras": int}
    )
    assert adapter.call(1, b=4, flag=5, a=9) == {
        "a": 1,
        "b": 4,
        "values": (),
        "flag": 5,
        "extras": {"a": 9},
    }
    assert adapter.call(1, 3, 5, 7, flag=8, z=11) == {
        "a": 1,
        "b": 3,
        "values": (5, 7),
        "flag": 8,
        "extras": {"z": 11},
    }
    with pytest.raises(PayloadValidationError):
        adapter.call(a=1)
