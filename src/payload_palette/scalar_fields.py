"""Closed stdlib scalar codecs for typed dataclass fields, never schema imports.

Only exact registered types and bounded ASCII wire strings are accepted. This
private table is not a user callback registry or permissive string coercion API.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import partial
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from typing import cast
from uuid import UUID

_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
_TIME = r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?"
_DURATION = re.compile(
    r"(-)?P(?:([0-9]{1,9})D)?(?:T(?:([0-9]{1,2})H)?(?:([0-9]{1,2})M)?"
    r"(?:([0-9]{1,2})(?:\.([0-9]{1,6}))?S)?)?"
)
_DECIMAL = re.compile(r"-?(?:[0-9]+(?:\.[0-9]+)?)(?:E[+-]?[0-9]{1,5})?")


def _temporal_text(value: object) -> str:
    item = cast(date | datetime | time, value)
    if type(item) in (datetime, time):
        temporal = cast(datetime | time, item)
        if temporal.fold != 0 or (
            temporal.tzinfo is not None and type(temporal.tzinfo) is not timezone
        ):
            raise ValueError("only fold-zero naive or fixed-offset temporal values are supported")
        if temporal.tzinfo is not None:
            offset = temporal.tzinfo.utcoffset(None)
            if offset is None or offset.microseconds or offset.seconds % 60:
                raise ValueError("timezone offset must use whole minutes")
    return item.isoformat()


def _parse_temporal(model: type[date] | type[time], text: str) -> object:
    pattern = _DATE if model is date else _TIME if model is time else _DATE + "T" + _TIME
    if re.fullmatch(pattern, text) is None:
        raise ValueError("temporal string does not use the fixed wire grammar")
    parsed = model.fromisoformat(text)
    expected = text[:-1] + "+00:00" if text.endswith("Z") else text
    if _temporal_text(parsed) != expected:
        raise ValueError("temporal string is not canonical")
    return parsed


def _duration_text(value: object) -> str:
    item = cast(timedelta, value)
    total = (item.days * 86400 + item.seconds) * 1_000_000 + item.microseconds
    sign = "-" if total < 0 else ""
    days, rest = divmod(abs(total), 86_400_000_000)
    hours, rest = divmod(rest, 3_600_000_000)
    minutes, rest = divmod(rest, 60_000_000)
    seconds, micros = divmod(rest, 1_000_000)
    output = sign + "P" + (f"{days}D" if days else "")
    clock = (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
    if seconds or micros:
        fraction = ("." + f"{micros:06d}".rstrip("0")) if micros else ""
        clock += f"{seconds}{fraction}S"
    if clock:
        output += "T" + clock
    return "PT0S" if total == 0 else output


def _parse_duration(text: str) -> object:
    match = _DURATION.fullmatch(text)
    if match is None:
        raise ValueError("invalid bounded ISO duration")
    sign, day, hour, minute, second, fraction = match.groups()
    days, hours, minutes, seconds = (int(value or "0") for value in (day, hour, minute, second))
    if hours >= 24 or minutes >= 60 or seconds >= 60:
        raise ValueError("duration clock components exceed their normalized ranges")
    micros = int((fraction or "").ljust(6, "0"))
    total = ((days * 86400 + hours * 3600 + minutes * 60 + seconds) * 1_000_000) + micros
    result = timedelta(microseconds=-total if sign else total)
    if _duration_text(result) != text:
        raise ValueError("duration is not canonical")
    return result


def _decimal_text(value: object) -> str:
    item = cast(Decimal, value)
    if not item.is_finite():
        raise ValueError("decimal must be finite")
    parts = item.as_tuple()
    exponent = cast(int, parts.exponent)
    if len(parts.digits) > 1000 or not -10_000 <= exponent <= 10_000:
        raise ValueError("decimal coefficient or exponent exceeds its fixed bound")
    # Decimal.__str__ consults the ambient context's capitals setting. Encode
    # from the exact tuple instead, retaining sign, coefficient and scale.
    digits = "".join(str(digit) for digit in parts.digits)
    adjusted = exponent + len(digits) - 1
    if exponent == 0:
        body = digits
    elif exponent < 0 and adjusted >= -6:
        point = len(digits) + exponent
        body = digits[:point] + "." + digits[point:] if point > 0 else "0." + "0" * -point + digits
    else:
        mantissa = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        body = mantissa + "E" + ("+" if adjusted >= 0 else "") + str(adjusted)
    return ("-" if parts.sign else "") + body


def _parse_decimal(text: str) -> object:
    if _DECIMAL.fullmatch(text) is None:
        raise ValueError("invalid finite decimal grammar")
    # String construction and as_tuple are exact; no arithmetic, normalize,
    # quantize or ambient-context precision/trap mutation is performed.
    value = Decimal(text)
    if _decimal_text(value) != text:
        raise ValueError("decimal string is not canonical")
    return value


def _parse_uuid(text: str) -> object:
    value = UUID(text)
    if str(value) != text:
        raise ValueError("UUID requires lowercase hyphenated canonical text")
    return value


_IP_TYPES = (IPv4Address, IPv6Address, IPv4Network, IPv6Network, IPv4Interface, IPv6Interface)


def _parse_ip(model: Callable[[str], object], text: str) -> object:
    if "%" in text:
        raise ValueError("scoped IPv6 addresses are not part of the wire contract")
    value = model(text)
    if str(value) != text:
        raise ValueError("IP value requires canonical address/prefix text")
    return value


@dataclass(frozen=True, slots=True)
class _ScalarCodec:
    model: type[object]
    name: str
    maximum: int
    parse: Callable[[str], object]
    format: Callable[[object], str]

    def decode(self, value: object) -> object:
        if type(value) is not str or not 1 <= len(value) <= self.maximum or not value.isascii():
            raise ValueError("scalar wire value must be a bounded ASCII string")
        return self.parse(value)

    def encode(self, value: object) -> str:
        if type(value) is not self.model:
            raise ValueError("scalar object must have the exact declared type")
        text = self.format(value)
        # Also refuse mutated Python IP objects with invalid internal fields.
        self.decode(text)
        return text


_CODECS = (
    _ScalarCodec(date, "date", 10, lambda text: _parse_temporal(date, text), _temporal_text),
    _ScalarCodec(
        datetime, "datetime", 32, lambda text: _parse_temporal(datetime, text), _temporal_text
    ),
    _ScalarCodec(time, "time", 21, lambda text: _parse_temporal(time, text), _temporal_text),
    _ScalarCodec(timedelta, "timedelta", 40, _parse_duration, _duration_text),
    _ScalarCodec(Decimal, "decimal", 1024, _parse_decimal, _decimal_text),
    _ScalarCodec(UUID, "uuid", 36, _parse_uuid, str),
    *(
        _ScalarCodec(model, model.__name__, 64, partial(_parse_ip, model), str)
        for model in _IP_TYPES
    ),
)


def _codec_for(annotation: object) -> _ScalarCodec | None:
    return next((codec for codec in _CODECS if annotation is codec.model), None)
