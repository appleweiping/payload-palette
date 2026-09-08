"""Offline typed model construction with precise values and canonical output."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from ipaddress import IPv6Address
from uuid import UUID

from payload_palette import DataclassAdapter


@dataclass(frozen=True, slots=True)
class Measurement:
    observed_at: datetime
    amount: Decimal
    device_id: UUID
    address: IPv6Address
    lifetime: timedelta = field(default_factory=lambda: timedelta(minutes=5))


def main() -> None:
    adapter = DataclassAdapter(Measurement)
    value = adapter.validate_python(
        {
            "observed_at": "2026-09-08T12:00:00Z",
            "amount": "12345678901234567890.0100",
            "device_id": "12345678-1234-5678-1234-567812345678",
            "address": "2001:db8::17",
        }
    )
    if type(value.observed_at) is not datetime or type(value.amount) is not Decimal:
        raise RuntimeError("typed fields were not constructed")
    wire = adapter.dump_json(value)
    if adapter.validate_json_bytes(wire) != value:
        raise RuntimeError("typed canonical roundtrip changed the model")
    print(wire.decode("utf-8"))
    print("typed datetime/Decimal/UUID/IPv6Address/timedelta roundtrip: OK")


if __name__ == "__main__":
    main()
