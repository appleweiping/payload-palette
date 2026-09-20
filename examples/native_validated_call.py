"""Offline declared native-call validation with identity and async checks."""

import asyncio
import inspect
from decimal import Decimal

from payload_palette import PayloadValidationError, native_validated_call


@native_validated_call(trust_annotations=True, validate_return=True)
def quote(amount: Decimal, /, count: int = 1) -> Decimal:
    return amount * count


@native_validated_call(trust_annotations=True, validate_return=True)
async def echo(value: list[int]) -> list[int]:
    await asyncio.sleep(0)
    return value


async def main() -> None:
    amount = Decimal("12.50")
    assert quote(amount, 2) == Decimal("25.00")
    try:
        quote("12.50", 2)  # type: ignore[arg-type]
    except PayloadValidationError:
        pass
    else:
        raise RuntimeError("strict native call accepted a wire string")
    value = [1, 2]
    assert inspect.iscoroutinefunction(echo)
    assert await echo(value) is value
    print("native_validated_call: strict sync/async oracle passed")


if __name__ == "__main__":
    asyncio.run(main())
