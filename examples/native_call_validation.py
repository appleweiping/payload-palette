"""Offline native argument binding, identity, async return and failure boundaries."""

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import cast

from payload_palette import CallAdapter, PayloadValidationError


@dataclass(frozen=True, slots=True)
class Measurement:
    day: date
    amount: Decimal


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_example() -> dict[str, object]:
    measurement = Measurement(date(2026, 9, 12), Decimal("123.4500"))
    labels = ["sample"]
    entries: list[str] = []

    def record(
        value: Measurement, /, positions: tuple[int, ...], *, tags: list[str] = labels
    ) -> Measurement:
        entries.append("sync")
        expect(value is measurement, "measurement identity changed")
        expect(tags is labels, "default identity changed")
        expect(positions == (2, 5), "tuple binding changed")
        return value

    adapter = CallAdapter(
        record,
        annotations={
            "value": Measurement,
            "positions": tuple[int, ...],
            "tags": list[str],
            "return": Measurement,
        },
        validate_return=True,
    )
    accepted = cast(Measurement, adapter.call(measurement, (2, 5)))
    expect(
        accepted is measurement and accepted.amount is measurement.amount, "return identity changed"
    )
    try:
        adapter.call(measurement, [2, 5])
    except PayloadValidationError:
        entries.append("rejected-input")
    else:
        raise RuntimeError("wire list was silently converted into a tuple")

    async def echo(value: Measurement) -> Measurement:
        await asyncio.sleep(0)
        entries.append("async")
        return value

    asynchronous = CallAdapter(
        echo, annotations={"value": Measurement, "return": Measurement}, validate_return=True
    )
    expect(
        asyncio.run(asynchronous.call_async(measurement)) is measurement, "async identity changed"
    )

    def invalid_return() -> str:
        entries.append("side-effect")
        return "invalid"

    try:
        CallAdapter(invalid_return, annotations={"return": int}, validate_return=True).call()
    except PayloadValidationError:
        entries.append("rejected-return")
    else:
        raise RuntimeError("invalid native return was accepted")
    expected = ["sync", "rejected-input", "async", "side-effect", "rejected-return"]
    expect(entries == expected, "call ordering or non-rollback behavior changed")
    result: dict[str, object] = {
        "day": measurement.day.isoformat(),
        "amount": str(measurement.amount),
        "entries": entries,
        "identity_preserved": True,
        "default_labels": labels,
    }
    expect(
        result
        == {
            "day": "2026-09-12",
            "amount": "123.4500",
            "entries": expected,
            "identity_preserved": True,
            "default_labels": ["sample"],
        },
        "unexpected example result",
    )
    return result


if __name__ == "__main__":
    print(json.dumps(run_example(), ensure_ascii=False, sort_keys=True))
