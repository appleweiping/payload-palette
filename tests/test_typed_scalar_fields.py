"""Typed scalar fields must compose with real dataclasses, not just accept strings."""

from dataclasses import dataclass, field, make_dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from uuid import UUID

import pytest

from payload_palette import DataclassAdapter, PayloadValidationError


@pytest.mark.parametrize(
    ("annotation", "wire", "expected"),
    [
        (date, "2024-02-29", date(2024, 2, 29)),
        (datetime, "2024-02-29T12:34:56Z", datetime(2024, 2, 29, 12, 34, 56, tzinfo=UTC)),
        (time, "12:34:56.000001", time(12, 34, 56, 1)),
        (timedelta, "-PT0.000001S", timedelta(microseconds=-1)),
        (Decimal, "12345678901234567890.0100", Decimal("12345678901234567890.0100")),
        (
            UUID,
            "12345678-1234-5678-1234-567812345678",
            UUID("12345678-1234-5678-1234-567812345678"),
        ),
        (IPv4Address, "192.0.2.17", IPv4Address("192.0.2.17")),
        (IPv6Address, "2001:db8::17", IPv6Address("2001:db8::17")),
        (IPv4Network, "192.0.2.0/24", IPv4Network("192.0.2.0/24")),
        (IPv6Network, "2001:db8::/32", IPv6Network("2001:db8::/32")),
        (IPv4Interface, "192.0.2.17/24", IPv4Interface("192.0.2.17/24")),
        (IPv6Interface, "2001:db8::17/32", IPv6Interface("2001:db8::17/32")),
    ],
)
def test_real_typed_field_and_json_roundtrip(annotation, wire, expected):
    model = make_dataclass("Model", [("item", annotation)])
    adapter = DataclassAdapter(model)
    result = adapter.validate_python({"item": wire})
    assert type(result.item) is annotation and result.item == expected
    canonical = wire.removesuffix("Z") + "+00:00" if wire.endswith("Z") else wire
    assert adapter.dump_python(result) == {"item": canonical}
    assert adapter.validate_json_bytes(adapter.dump_json(result)) == result


def test_bad_supplied_scalar_precedes_every_factory_and_constructor():
    calls = []

    @dataclass
    class Model:
        day: date
        fallback: str = field(default_factory=lambda: calls.append("factory") or "default")

        def __post_init__(self):
            calls.append("constructor")

    adapter = DataclassAdapter(Model)
    with pytest.raises(PayloadValidationError):
        adapter.validate_python({"day": "2023-02-29"})
    assert calls == []


def test_offline_example_constructs_typed_values_and_roundtrips(capsys):
    import runpy
    from pathlib import Path

    runpy.run_path(
        str(Path(__file__).parents[1] / "examples" / "typed_scalar_fields.py"), run_name="__main__"
    )
    output = capsys.readouterr().out
    assert "typed datetime/Decimal/UUID/IPv6Address/timedelta roundtrip: OK" in output
    assert '"amount":"12345678901234567890.0100"' in output
