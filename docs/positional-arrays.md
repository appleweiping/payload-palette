# Positional arrays and typed tuple fields

The schema engine can validate different types at different array positions.
Both annotation adapters use this same engine; only `DataclassAdapter` constructs
actual tuples. The JSON-only `AnnotationAdapter` continues returning fresh lists.
Run `python examples/positional_arrays.py` for an offline end-to-end example.

## Prefix, suffix and required length are separate

```python
from payload_palette import OutputSchema, export_output_schema, load_output_schema

schema = OutputSchema(
    "array",
    prefix_items=(OutputSchema("integer"), OutputSchema("string")),
    items=OutputSchema("boolean"),
    min_length=2,
    max_length=4,
)
assert not schema.validate([7, "camera", True])
assert schema.validate([7, False])
assert load_output_schema(export_output_schema(schema)) == schema
```

`prefix_items=None` retains the existing homogeneous-array contract: `items`
must be an `OutputSchema`. An explicit tuple of zero to 256 schemas enables
positional validation. Each **present** prefix element is checked against the
schema at the same index. Beyond the prefix, `items=OutputSchema(...)` validates
every suffix element; `items=None` forbids all suffix elements.

A prefix alone does not require its positions to exist. A closed two-position
prefix accepts lengths zero, one and two unless `min_length` says otherwise.
An empty closed prefix accepts only `[]`. Length bounds apply to the complete
array, independently of element validation. Configurations whose combined
constraints have no satisfying value are not silently loosened.

Schema data uses Draft 2020-12 `prefixItems` (a nonempty array of supported
schemas) and explicit `items` (a supported schema or `false`). Omitting `items`
or supplying `true` is unsupported, not interpreted as a closed array.
An empty closed prefix exports as `{"type":"array","items":false}` without
invalid empty `prefixItems`. An empty prefix with a typed suffix normalizes to
a homogeneous array; its acceptance set is unchanged, not necessarily its
internal dataclass representation. The Draft specifies these separate roles in
[array applicators](https://json-schema.org/draft/2020-12/json-schema-core#section-10.3.1).
This remains a bounded subset, not support for every schema keyword.

## Python annotations and model construction

| Annotation | JSON representation | Dataclass field after validation |
|---|---|---|
| `tuple[int, str]` | Exactly two elements, in that order | Exact built-in two-element tuple |
| `tuple[int, ...]` | Any admitted number of strict integers | Exact built-in homogeneous tuple |
| `tuple[()]` | Empty array | Empty tuple |

Parameterized legacy `typing.Tuple` forms have the same behavior. Bare `tuple`
and `typing.Tuple`, named tuples, unpacked type variables and malformed ellipses
are rejected. Fixed tuples support at most 256 concrete element types. Length
metadata intersects the fixed shape; it cannot relax its exact length.
Variadic tuple length is bounded by the existing output graph limits and any
explicit `FieldConstraints`.

Wire input is exact JSON: neither adapter consumes arbitrary iterables or
coerces a Python tuple to a JSON list. Dataclass dumping instead requires an
exact built-in tuple where declared; a list or tuple subclass is not equivalent.
Tuple members compose with nested dataclasses, TypedDicts, lists/maps, unions
and the [typed scalar family](typed-scalar-fields.md).

Tuple-bearing unions must have one unambiguous matching wire branch before any
user factory or constructor runs. For example `tuple[int, ...] | list[int]`
is ambiguous for `[1]` and fails, even if a runtime tuple could identify a branch
while dumping. This preserves reloadable type selection. Scalar-bearing tuple
members also undergo pure semantic preflight before user callbacks.

Literal tuple defaults are projected and frozen when compiling the adapter;
mutable members are reconstructed separately for each call. Factory results
must have the declared tuple shape. Final projection checks the entire resulting
object graph after constructors and post-init methods, including tuple shape
and member types. Built-in tuple construction itself is not a user callback.
The existing [dataclass ownership and trusted-code limits](dataclass-adaptation.md)
still apply; arbitrary user side effects cannot be rolled back.

## Composition and resource accounting

Synchronous repairs, asynchronous final checks and generation feedback resolve
each integer path through its actual prefix or suffix schema. Impossible closed
positions, positions beyond `max_length`, and descent through scalar positions
are rejected at configuration time. A possible but absent optional position
still skips at runtime. Input arrays remain isolated from callback mutations.
Incremental JSON events stay provisional: schema and semantic checks still run
only at explicit successful EOF, not when a tuple-shaped prefix first arrives.

Every repeated prefix schema counts in expanded definition node/depth budgets.
Every validated element or rejected extra position costs schema work; exhausting
the work budget is an error, never acceptance. Existing node/character/issue,
adapter traversal/default/callback and encoded-byte limits are not relaxed.
These are bounded data/work contracts, not constant-memory processing or a
wall-clock sandbox. General iterable coercion, named-tuple construction,
unpacked/generic/recursive tuples and full-reference interoperability stay open.
