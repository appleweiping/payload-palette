# Runtime schema interchange and annotation adapters

This API compiles a deliberately bounded subset of schema data or trusted Python annotation objects
into the same immutable `OutputSchema` used by validation pipelines and generation. It does not
change request normalization or make network calls. Run the complete offline example:

```bash
python examples/runtime_schema_adapter.py
```

## Schema data contract

`load_output_schema(document)` accepts exact built-in dictionaries. `load_output_schema_json_bytes`
first uses the strict ingress decoder (4,000,000 bytes by default), rejecting duplicate JSON keys,
invalid UTF-8 and nonstandard numbers. Definition failures are structured `SchemaDefinitionError`
instances with stable issue codes and schema-definition paths; byte decoding failures retain
`PayloadValidationError`. Neither function evaluates strings, imports code, fetches references, or
silently ignores unknown or inapplicable keywords.

| Schema form | Supported constraints |
|---|---|
| `type: null / boolean` | Scalar `enum` or `const` |
| `type: integer / number` | Inclusive `minimum`, `maximum`, scalar `enum` or `const` |
| `type: string` | Unicode code-point `minLength`, `maxLength`, scalar `enum` or `const` |
| `type: array` | Explicit schema-valued or false `items`, optional positional `prefixItems`, `minItems`, `maxItems` |
| `type: object` | `properties`, `required`, boolean or schema-valued `additionalProperties` |
| `anyOf` | 2–16 explicit schema branches; only `$schema` may accompany it |
| `type: [...]` | 1–7 unique unconstrained scalar/object types; only `$schema` may accompany it |

Type lists normalize to branches. Arrays still require explicit `items`, so nullable arrays
use `anyOf`, not a bare array in a type list. Put branch-specific constraints inside `anyOf`.
Scalar enums contain 1–256 unique values matching their declared type. `const` normalizes to a
singleton enum; enum and const together are rejected. Required names must be declared properties.
These are deliberate restrictions even where broader JSON Schema permits another formulation.

Imported objects are **open by default**, following JSON Schema. Direct `OutputSchema` constructors
and TypedDict adapters are **closed by default**. Exports always state `additionalProperties`
explicitly. Typed maps validate every undeclared member through their additional-properties schema.
Shared acyclic schema dictionaries are copied; source mutation cannot alter a compiled schema.

The only accepted root `$schema` URI is `https://json-schema.org/draft/2020-12/schema`. It identifies
the source keyword semantics, not a claim to implement the complete dialect. Nested dialects,
general boolean schemas, empty schemas, `$ref`, `$defs`, recursion, `oneOf`, `allOf`, discriminators,
regex/format, dependent conditions, arbitrary extensions and custom vocabulary are
rejected. Resource/finite-number restrictions also remain part of Payload's runtime contract.
The specific `items: false` form closes the suffix after any declared prefix; it is not general
boolean-schema support. See [positional arrays](positional-arrays.md) for length semantics.
See the first-party descriptions of [schema combination](https://json-schema.org/understanding-json-schema/reference/combining)
and [numeric types](https://json-schema.org/understanding-json-schema/reference/numeric).

## Integer representation and faithful exchange

JSON Schema's `integer` is mathematical: both `1` and `1.0` are integer values. Imported integer
schemas therefore use `integer_mode="json"`, accepting finite integral floats without coercing
them, and comparing integer enum/const values numerically. Booleans and fractional floats are
rejected. A `number` schema likewise compares enums numerically, independently of representation.

Python `int` annotations and direct integer schemas use `integer_mode="strict"`, accepting only
exact built-in ints. `export_output_schema(schema)` produces fresh normalized schema data with
`x-payload-strict-integer: true` on strict integer nodes, preserving that distinction when reloaded
by Payload. This documented extension is accepted only on integer schemas and only with value
`true`. Other JSON Schema engines may ignore it and therefore accept a broader set of Python
representations: the extension is **not portable validation behavior**.

The existing `schema.json_schema()` returns a portable keyword projection without that extension,
useful for a provider's schema hint. It cannot encode the Python int/float distinction. Use
`schema.json_schema(preserve_python_types=True)` or `export_output_schema` for Payload interchange.
No exported schema encodes every runtime byte/work budget; final local validation remains required.

## Trusted annotation compilation

```python
from typing import Annotated, NotRequired, TypedDict
from payload_palette import AnnotationAdapter, FieldConstraints


class Result(TypedDict):
    label: Annotated[str, FieldConstraints(min_length=1, max_length=120)]
    confidence: Annotated[float, FieldConstraints(minimum=0, maximum=1)] | None
    note: NotRequired[str]


adapter = AnnotationAdapter(Result)
value = adapter.validate_json_bytes(b'{"label":"bicycle","confidence":null}')
encoded = adapter.dump_json(value)
```

This example uses live Python annotation objects. If a module enables postponed string annotations,
use explicit objects in functional `TypedDict("Result", {...})`, as the executable example does.
Strings and `ForwardRef` objects are rejected, not passed to `eval` or `get_type_hints`. Type objects
are trusted Python configuration, not a sandbox; obtaining Python class metadata can use Python's
own runtime machinery.

Supported annotations are exact `str`, `int`, `float`, `bool`, `None`/`NoneType`, `list[T]`,
fixed `tuple[T1, T2, ...]` (concrete element types), variadic `tuple[T, ...]`, empty `tuple[()]`,
`dict[str, T]`, PEP 604/`typing.Union`, JSON-scalar `Literal`, and `TypedDict` including inherited
fields, total/partial records and direct `Required`/`NotRequired` field wrappers. Literals support
strings, ints, booleans and null, separating bool from int. A float annotation describes JSON's
number family; an accepted integer remains an integer. Values are never coerced into another type.

Exactly one `FieldConstraints` object is accepted as `Annotated` metadata. Numeric bounds apply to
integer/number schemas; length bounds apply to strings and arrays. Put metadata on a concrete
branch before making it nullable. Stacked metadata flattened by Python, unknown metadata, and
constraints on incompatible kinds are explicit errors rather than silently overwritten settings.

`AnnotationAdapter` returns isolated JSON data, **not** class instances. The distinct
[`DataclassAdapter`](dataclass-adaptation.md) supports explicit bounded stdlib dataclass construction.
For `AnnotationAdapter` itself, dataclass/model construction,
arbitrary validators/serializers, aliases, default injection, coercion, `Any`, untyped containers,
named tuples/sets, non-string map keys, recursive references, generic models, bytes, date/network types and
call validation are not implemented. See Python's [typing contract](https://docs.python.org/3/library/typing.html)
for the source annotation forms; this adapter intentionally supports only the subset above.

## Serialization and budgets

`JSONSerializationOptions` configures `ensure_ascii=False`, `sort_keys=True`, `indent=None` and
`max_output_bytes=4_000_000`. Indent may be 0–8; output bytes may be 1–32,000,000. Serialization
validates first and preserves fields and JSON numeric representations. It measures escaped/UTF-8
output incrementally before encoding each chunk; the resulting byte cap is exact. This caps returned
bytes, not the JSON encoder's internal temporary string allocations. Existing snapshot character
bounds constrain those inputs. No callback serializer or user constructor is invoked.

Definition limits independently bound expanded schema depth (32), compiler/schema nodes (4096),
property/enum characters (1,000,000) and branches per union (16); these defaults are also ceilings.
Callers may lower them, with depth 0 permitting only a leaf definition and at least two union
branches. Each repeated shared child counts again. Annotation compiler visits (including metadata)
and its final expanded schema are both checked. Object properties and enums additionally cap at
256 entries, property names at 256 characters and enum strings at 2048 characters.

`OutputLimits.max_schema_steps` defaults to 100,000, with a hard ceiling of 1,000,000. Every visited
schema/value pair consumes one step, shared across candidate union branches in a validation pass;
failed branches cannot reset work. Budget exhaustion raises `OutputContractError`, never a success
or ordinary union mismatch. Existing JSON depth/nodes/characters and issue limits still apply.
Pipeline initial/final validations each have their own schema-work pass; callback budgets remain
separate. There is no wall-clock preemption of local Python schema execution.

## Pipeline and generation behavior

A binding path is configured successfully only if it is possible in at least one union branch.
At runtime it skips a genuinely absent optional/alternative path. It cannot silently bypass a typo
in a closed object or descend below a known scalar or typed map scalar. Unknown open-object shapes
remain explicitly permissive. Repairs still undergo complete final schema and semantic validation:
switching a union's valid branch does not waive other applicable final rules.

Generation sends the portable schema projection as a hint and validates locally afterward. Re-ask
feedback permits only paths declared in the schema (including union branches); arbitrary keys in
open/typed maps remain redacted to `$`. Schema importing does not import a validator pipeline or
provider configuration. Cross-engine corpus/performance equivalence and whole-reference repository
parity remain open, as recorded in [the reference assessment](parity-validation.md).
