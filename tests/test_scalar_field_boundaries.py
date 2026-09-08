"""Independent canonical-wire, composition and callback-order boundaries."""

import random
from dataclasses import dataclass, field, make_dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal, Inexact, InvalidOperation, Rounded, localcontext
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from typing import Annotated, TypedDict
from uuid import UUID

import pytest

from payload_palette import (
    AnnotationAdapter,
    DataclassAdapter,
    DataclassLimits,
    FieldConstraints,
    OutputContractError,
    OutputLimits,
    PayloadValidationError,
)
from payload_palette.schema_io import SchemaDefinitionError


def adapter(annotation, **kwargs):
    return DataclassAdapter(make_dataclass("Model", [("value", annotation)]), **kwargs)


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        (date, "2023-02-29"),
        (date, "20240229"),
        (date, "2024-W09-4"),
        (date, "2024-2-29"),
        (date, "0000-01-01"),
        (date, "10000-01-01"),
        (date, "\uff12\uff10\uff12\uff14-02-29"),
        (date, "2024-02-29T00:00:00"),
        (datetime, "2024-02-29 12:34:56"),
        (datetime, "2024-02-29T12:34"),
        (datetime, "2024-02-29T24:00:00"),
        (datetime, "2024-02-29T12:34:60"),
        (datetime, "2024-02-29T12:34:56.1"),
        (datetime, "2024-02-29T12:34:56.000000"),
        (datetime, "2024-02-29T12:34:56-00:00"),
        (datetime, "2024-02-29T12:34:56+24:00"),
        (datetime, "2024-02-29T12:34:56+01:00:30"),
        (time, "123456"),
        (time, "12:34:56.1234567"),
        (time, "12:34:56,123456"),
        (time, "12:34:56z"),
        (time, "12:34:56+00:00:01"),
        (timedelta, "P"),
        (timedelta, "PT"),
        (timedelta, "P0D"),
        (timedelta, "P1DT"),
        (timedelta, "PT24H"),
        (timedelta, "PT60M"),
        (timedelta, "PT60S"),
        (timedelta, "PT01S"),
        (timedelta, "PT1.0S"),
        (timedelta, "-PT0S"),
        (timedelta, "P1M"),
        (timedelta, "P1Y"),
        (timedelta, "P1W"),
        (timedelta, "PT0.0000001S"),
        (timedelta, "-P999999999DT0.000001S"),
        (Decimal, "NaN"),
        (Decimal, "sNaN"),
        (Decimal, "Infinity"),
        (Decimal, "+1"),
        (Decimal, "01"),
        (Decimal, "1."),
        (Decimal, ".1"),
        (Decimal, "1e3"),
        (Decimal, "1E100"),
        (Decimal, "1E+10001"),
        (Decimal, "1E-10001"),
        (Decimal, "1_000"),
        (Decimal, " 1"),
        (Decimal, "9" * 1001),
        (Decimal, "1E+123456"),
        (UUID, "12345678123456781234567812345678"),
        (UUID, "{12345678-1234-5678-1234-567812345678}"),
        (UUID, "ABCDEFAB-1234-5678-1234-567812345678"),
        (IPv4Address, "192.000.2.1"),
        (IPv4Address, "3221225985"),
        (IPv4Address, "192.0.2.256"),
        (IPv6Address, "2001:DB8::1"),
        (IPv6Address, "2001:db8:0:0:0:0:0:1"),
        (IPv6Address, "fe80::1%eth0"),
        (IPv4Network, "192.0.2.1/24"),
        (IPv4Network, "192.0.2.0/255.255.255.0"),
        (IPv6Network, "2001:db8::1/32"),
        (IPv6Network, "2001:db8::/129"),
        (IPv4Interface, "192.0.2.1"),
        (IPv6Interface, "fe80::1%eth0/64"),
    ],
)
def test_invalid_noncanonical_or_out_of_domain_scalar(annotation, value):
    with pytest.raises(PayloadValidationError):
        adapter(annotation).validate_python({"value": value})


@pytest.mark.parametrize(
    "annotation", [date, datetime, time, timedelta, Decimal, UUID, IPv4Address]
)
@pytest.mark.parametrize("value", [None, True, 1, 1.2, [], {}, "", "x" * 1025])
def test_scalars_never_accept_implicit_number_object_or_unbounded_text_coercion(annotation, value):
    with pytest.raises(PayloadValidationError):
        adapter(annotation).validate_python({"value": value})


def test_nested_types_defaults_factories_and_constructor_receive_actual_values():
    seen = []

    class Network(TypedDict):
        address: IPv6Address

    @dataclass(frozen=True, slots=True)
    class Reading:
        instant: datetime
        amount: Decimal

        def __post_init__(self):
            seen.append((type(self.instant), type(self.amount)))

    @dataclass
    class Envelope:
        readings: list[Reading]
        network: Network
        ids: dict[str, UUID]
        missing: date | None = None
        default_day: date = date(2024, 2, 29)
        ttl: timedelta = field(default_factory=lambda: timedelta(seconds=1))

    compiled = DataclassAdapter(Envelope)
    result = compiled.validate_python(
        {
            "readings": [{"instant": "2024-02-29T00:00:00Z", "amount": "1.00"}],
            "network": {"address": "::1"},
            "ids": {"first": "00000000-0000-0000-0000-000000000000"},
        }
    )
    assert seen == [(datetime, Decimal)]
    assert result.default_day == date(2024, 2, 29) and result.ttl == timedelta(seconds=1)
    assert type(result.network["address"]) is IPv6Address
    assert type(result.ids["first"]) is UUID and result.missing is None
    before = list(seen)
    assert compiled.dump_python(result)["readings"][0]["amount"] == "1.00"
    assert seen == before


def test_scalar_unions_use_pure_semantic_preflight_and_still_reject_ambiguity():
    compiled = adapter(date | UUID)
    assert type(compiled.validate_python({"value": "2024-02-29"}).value) is date
    assert (
        type(compiled.validate_python({"value": "00000000-0000-0000-0000-000000000000"}).value)
        is UUID
    )
    with pytest.raises(PayloadValidationError):
        compiled.validate_python({"value": "not either type"})
    ambiguous = adapter(date | str)
    with pytest.raises(PayloadValidationError, match="ambiguous"):
        ambiguous.validate_python({"value": "2024-02-29"})
    assert ambiguous.validate_python({"value": "label"}).value == "label"
    with pytest.raises(PayloadValidationError, match="ambiguous"):
        ambiguous.dump_python(ambiguous.model(date(2024, 2, 29)))


def test_dataclass_union_with_different_scalar_types_never_trials_constructors():
    calls = []

    @dataclass
    class Day:
        value: date

        def __post_init__(self):
            calls.append("day")

    @dataclass
    class Identifier:
        value: UUID

        def __post_init__(self):
            calls.append("id")

    compiled = adapter(Day | Identifier)
    result = compiled.validate_python({"value": {"value": "2024-02-29"}})
    assert type(result.value) is Day and calls == ["day"]


def test_decimal_is_exact_under_hostile_ambient_precision_traps_and_preserves_scale():
    compiled = adapter(Decimal)
    with localcontext() as context:
        context.prec = 1
        context.Emax = 2
        context.Emin = -2
        context.capitals = 0
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        context.traps[InvalidOperation] = True
        flags = dict(context.flags)
        for wire in ("1234567890.012300", "-0.00", "1E+10000", "1E-10000"):
            result = compiled.validate_python({"value": wire})
            assert compiled.dump_python(result) == {"value": wire}
            assert result.value.as_tuple() == Decimal(wire).as_tuple()
        assert dict(context.flags) == flags and context.prec == 1


@pytest.mark.parametrize(
    "value", [timedelta.min, timedelta.max, timedelta(), timedelta(microseconds=-1)]
)
def test_duration_extrema_roundtrip_without_float_precision_loss(value):
    compiled = adapter(timedelta)
    typed = compiled.model(value)
    assert compiled.validate_json_bytes(compiled.dump_json(typed)) == typed


def test_custom_timezone_is_rejected_without_executing_its_methods():
    class CustomZone(tzinfo):
        def utcoffset(self, dt):
            pytest.fail("custom timezone must not be invoked")

    compiled = adapter(datetime)
    value = datetime(2024, 1, 1, tzinfo=CustomZone())
    with pytest.raises(PayloadValidationError):
        compiled.dump_python(compiled.model(value))


@pytest.mark.parametrize("annotation", [time, datetime])
def test_fold_and_subminute_offsets_are_not_silently_lost(annotation):
    compiled = adapter(annotation)
    base = time(1) if annotation is time else datetime(2024, 1, 1)
    for value in (base.replace(fold=1), base.replace(tzinfo=timezone(timedelta(seconds=1)))):
        with pytest.raises(PayloadValidationError):
            compiled.dump_python(compiled.model(value))


def test_annotated_string_lengths_intersect_intrinsic_codec_bound():
    constrained = Annotated[date, FieldConstraints(min_length=0, max_length=100)]
    compiled = adapter(constrained)
    assert compiled.input_schema.properties["value"].min_length == 1
    assert compiled.input_schema.properties["value"].max_length == 10
    assert type(compiled.validate_python({"value": "2024-02-29"}).value) is date
    with pytest.raises(SchemaDefinitionError):
        adapter(Annotated[date, FieldConstraints(min_length=11)])


def test_output_character_budget_accounts_for_utc_expansion_before_constructor():
    calls = []

    @dataclass
    class Model:
        x: datetime

        def __post_init__(self):
            calls.append(True)

    compiled = DataclassAdapter(Model, limits=OutputLimits(max_characters=21))
    with pytest.raises(OutputContractError):
        compiled.validate_python({"x": "2024-01-01T00:00:00Z"})
    assert calls == []


def test_old_json_annotation_adapter_does_not_acquire_typed_constructors():
    for annotation in (date, datetime, Decimal, UUID, IPv4Address):
        with pytest.raises(SchemaDefinitionError):
            AnnotationAdapter(annotation)


def test_work_exhaustion_is_not_swallowed_as_union_mismatch():
    compiled = adapter(date | UUID, construction=DataclassLimits(max_steps=5))
    with pytest.raises(OutputContractError):
        compiled.validate_python({"value": "2024-02-29"})


@pytest.mark.parametrize("raw", ["NaN", "sNaN", "Infinity", "-Infinity", "9" * 1001, "1E+10001"])
def test_invalid_typed_decimal_is_rejected_on_dump_and_literal_default(raw):
    compiled = adapter(Decimal)
    value = Decimal(raw)
    with pytest.raises(PayloadValidationError):
        compiled.dump_python(compiled.model(value))
    model = make_dataclass("Default", [("value", Decimal, field(default=value))])
    with pytest.raises(PayloadValidationError):
        DataclassAdapter(model)


def test_decimal_encoder_matches_independent_stdlib_canonical_oracle_for_seeded_tuples():
    compiled = adapter(Decimal)
    rng = random.Random(75119)
    for index in range(250):
        length = rng.choice([1, 2, 7, 40, 1000])
        digits = tuple(rng.randrange(10) for _ in range(length))
        exponent = rng.choice([-10000, -100, -7, -6, -1, 0, 1, 5, 100, 10000])
        value = Decimal((index % 2, digits, exponent))
        with localcontext() as oracle:
            oracle.capitals = 1
            expected = str(value)
        with localcontext() as hostile:
            hostile.capitals = 0
            hostile.prec = 1
            actual = compiled.dump_python(compiled.model(value))["value"]
            assert actual == expected
            restored = compiled.validate_python({"value": actual}).value
            assert restored.as_tuple() == value.as_tuple()


def test_temporal_extrema_and_fixed_offsets_preserve_value_and_offset():
    for annotation, values in (
        (date, [date.min, date.max]),
        (
            time,
            [
                time.min,
                time.max,
                time(1, 2, 3, 4, tzinfo=timezone(timedelta(hours=-23, minutes=-59))),
            ],
        ),
        (
            datetime,
            [
                datetime.min,
                datetime.max,
                datetime(2024, 3, 10, 1, 30, tzinfo=timezone(timedelta(hours=-6))),
            ],
        ),
    ):
        compiled = adapter(annotation)
        for value in values:
            typed = compiled.model(value)
            assert compiled.validate_json_bytes(compiled.dump_json(typed)) == typed


def test_post_init_scalar_mutation_is_revalidated_without_returning_false_acceptance():
    @dataclass
    class Model:
        value: date

        def __post_init__(self):
            self.value = "2024-02-29"

    with pytest.raises(PayloadValidationError):
        DataclassAdapter(Model).validate_python({"value": "2024-02-29"})


def test_factory_scalar_requires_typed_value_and_never_runs_adapter_constructor_on_failure():
    calls = []

    @dataclass
    class Model:
        value: date = field(default_factory=lambda: "2024-02-29")

        def __post_init__(self):
            calls.append(True)

    with pytest.raises(PayloadValidationError):
        DataclassAdapter(Model).validate_python({})
    assert calls == []


def test_subclasses_and_scoped_ipv6_objects_are_not_silently_normalized():
    class Subdate(date):
        pass

    for annotation, value in (
        (date, Subdate(2024, 1, 1)),
        (IPv6Address, IPv6Address("fe80::1%eth0")),
    ):
        compiled = adapter(annotation)
        with pytest.raises(PayloadValidationError):
            compiled.dump_python(compiled.model(value))


def test_published_validation_issue_does_not_echo_private_scalar_input():
    secret = "private-secret-not-a-uuid"
    with pytest.raises(PayloadValidationError) as caught:
        adapter(UUID).validate_python({"value": secret})
    assert secret not in str(caught.value) and "dataclass_scalar" in str(caught.value)
