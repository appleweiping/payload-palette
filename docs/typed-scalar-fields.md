# Strict standard-library fields in dataclasses

`DataclassAdapter` now composes twelve concrete standard-library scalar types
with nested dataclasses, lists, string-key maps, TypedDict, nullable fields,
unions and defaults. They return actual typed objects. `AnnotationAdapter`
continues to accept and return JSON only; it does not acquire constructors.

Run `python examples/typed_scalar_fields.py` for an entirely offline example.

## Wire types and exact scope

Every scalar input is a built-in ASCII string. Numbers, bytes, instances of the
target class and permissive coercion hooks are not wire input. Dumping requires
the exact registered type, not subclasses or string-shaped substitutes.

| Field annotation | Accepted wire representation | Text ceiling |
| --- | --- | ---: |
| `datetime.date` | `YYYY-MM-DD`, real Gregorian date, years 1 through 9999 | 10 |
| `datetime.datetime` | Date, literal `T`, time below, optional fixed offset | 32 |
| `datetime.time` | `HH:MM:SS`, optional nonzero six-digit fractional seconds, optional `Z` or `+/-HH:MM` | 21 |
| `datetime.timedelta` | Normalized signed day/hour/minute/second duration such as `P2DT3H4M5.000006S`, `-PT0.000001S` or `PT0S` | 40 |
| `decimal.Decimal` | Canonical finite coefficient/scale string such as `123.4500`, `-0.00`, `1E+10000` | 1024 |
| `uuid.UUID` | Lowercase, hyphenated 36-character UUID; no version restriction | 36 |
| `ipaddress.IPv4Address`, `IPv6Address` | Canonical dotted IPv4 or compressed lowercase IPv6 | 64 |
| `ipaddress.IPv4Network`, `IPv6Network` | Canonical address/prefix; host bits must already be zero | 64 |
| `ipaddress.IPv4Interface`, `IPv6Interface` | Canonical host address/prefix; host bits are retained | 64 |

Each parser validates the real domain, not only the text shape. Whitespace,
alternate date/week forms, integer timestamps, leap seconds, uppercase or
uncompressed IP/UUID forms, IPv6 scopes, address netmasks and omitted interface
prefixes are rejected. An empty string is never a scalar value.

Time fractions have exactly six digits when present and must not be all zero.
UTC `Z` is the one accepted alternate spelling: dumping normalizes it to
`+00:00`. Otherwise temporal text must equal the fixed ISO representation.
Naive values remain naive; no local timezone is guessed. Only `fold=0` and
`None`/exact `datetime.timezone` tzinfo are supported. Offsets use whole minutes
and keep their original offset instead of converting to UTC. Named-zone identity,
custom timezone code and the display name of a fixed offset are not serialized.
Custom tzinfo methods are rejected before invocation. See Python's
[temporal types](https://docs.python.org/3/library/datetime.html) for their
underlying calendar, fixed-offset and microsecond semantics.

Durations use integer microseconds throughout, including the full asymmetric
stdlib `timedelta.min`/`max` range. There is no float conversion. Clock components
are normalized (`hours<24`, `minutes<60`, `seconds<60`); zero components are
omitted, fractional trailing zeros are removed and zero is exactly `PT0S`.
Weeks, months and years are not inferred. `PT60S`, `P0D`, `-PT0S` and `PT1.0S`
are rejected rather than normalized silently.

Decimals retain their sign, exact coefficient and trailing-zero scale, including
negative zero. The coefficient has at most 1000 digits and its **stored tuple
exponent** is between -10000 and 10000; the displayed scientific exponent can
differ because it also incorporates coefficient length. Encoding is derived
from that tuple, independent of ambient precision, rounding, traps and the
`capitals` setting. No Decimal arithmetic, quantization or context mutation is
performed. NaNs/infinities are unsupported. The fixed/scientific boundary is
the standard canonical one: nonpositive exponents with adjusted exponent at
least -6 use fixed notation, otherwise scientific notation with uppercase `E`.
Python's [Decimal tuple](https://docs.python.org/3/library/decimal.html#decimal.Decimal.as_tuple)
is the exact coefficient/scale basis, not a binary float conversion.

IP handling is parsing only: no DNS lookup, socket connection or network call.
Network host bits are checked, not masked away. The precise address/network/
interface distinction follows the corresponding
[stdlib types](https://docs.python.org/3/library/ipaddress.html). UUIDs are parsed
as [UUID values](https://docs.python.org/3/library/uuid.html#uuid.UUID); generation,
version policies and identity authentication are not part of this feature.

## Construction, defaults and unions

All supplied scalar values are semantically preflighted before factories or
adapter constructors run. Factories must return already typed values for their
declared fields; a date factory returning a date-shaped string is invalid.
Literal defaults are encoded and frozen at compilation, then reconstructed per
call. Final model projection rechecks every scalar, so a post-init replacing a
date with a string cannot return an accepted model. The
[existing constructor/ownership contract](dataclass-adaptation.md) still applies.

Unions with scalar conversion use pure, bounded semantic preflight to distinguish
branches without trial constructors. `date | UUID` can distinguish its two wire
formats. `date | str` is deliberately ambiguous for a valid date string, since
both branches accept it; unrelated strings select `str`. Nested dataclass unions
with scalar fields follow the same rule. Budget exhaustion is never downgraded
to a candidate mismatch. Branches without scalar conversion retain their existing
structural matching behavior and work accounting.

`Annotated[T, FieldConstraints(...)]` length bounds intersect the codec's intrinsic
string limits; a looser requested maximum cannot disable admission. Numeric
bounds on these string-encoded types are unsupported. Semantic date ranges,
Decimal precision policies, allowed UUID versions and network membership need
explicit application logic; this feature does not silently invent those policies.

## Schemas, work and trust boundaries

`input_schema` and `output_schema` expose closed object structure and bounded
string shapes. Portable JSON Schema exports **do not encode these scalar parsers**
or their canonical grammar as `format`/`pattern` validators. Passing only the
projected schema to another engine cannot certify a typed scalar. Apply the
dataclass adapter locally to finish the contract; imports do not load classes,
codecs or constructors from a schema document.

The existing definition, node/depth, character, serialized-byte, schema-step and
construction-step limits still apply. Every recursive scalar visit consumes work;
candidate union branches share the same monotonic counters. Preparation counts
the canonical output text, including UTC's `Z` to `+00:00` expansion, before
calling adapter constructors. Factory and constructor budgets count explicit
user callbacks, not fixed built-in scalar parsing. No custom codec registry is
enabled.

Wire lengths are checked before parser invocation. Dumping caller-held Decimal
objects inspects `as_tuple()` before rejecting an oversized coefficient: that
introspection can take time/space proportional to the existing coefficient,
not the accepted 1000-digit ceiling. That temporary tuple, existing caller objects,
trusted constructors and native-library
allocations are not bounded RSS. This API does not preempt Python callbacks,
promise external side-effect rollback or authenticate IP/UUID identities.
Public scalar issues contain a path and fixed type label, not the supplied value;
Python exception objects/tracebacks are not complete process-memory redaction.

## Verification and remaining scope

Independent tests include canonical/noncanonical domain vectors, temporal and
duration extrema, constructor/default ordering, pure union selection, character
expansion admission, hostile Decimal contexts and 250 seeded Decimal tuples
compared with the separately configured stdlib representation. Existing JSON
adapter behavior is tested alongside the typed adapter. Final full-suite,
package and hosted results are recorded separately when actually run.

URL/email/path, Enum, Fraction, bytes, tuple/set, recursive/generic models,
aliases and configurable serializers remain open, along with the broader
[whole-reference assessment](parity-validation.md). The frozen reference's
[standard-library type surface](https://github.com/pydantic/pydantic/blob/2261ae19e2e09f792f06613360c83fc829238111/docs/api/standard_library_types.md)
includes a broader set of conversions and policies. This original strict-wire
implementation is not a compatibility claim for that full surface.
