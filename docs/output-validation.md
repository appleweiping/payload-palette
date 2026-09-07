# Structured output validation

The output API validates a complete generated JSON value. It complements the existing request
normalizer; it does not change accepted request envelopes, MIME checks, remote URL policy, or media
streaming. It runs locally with no runtime dependencies. The executable entry point is:

```bash
python examples/validate_model_output.py
```

## Schema contract

`OutputSchema` accepts `null`, `boolean`, `integer`, `number`, `string`, `array`, `object`, and `union`.
There is no implicit coercion. By default, booleans are not numbers, `1.0` is not an integer,
and `"0.8"` is not a number. Numbers must be finite. Integers have at most 850 magnitude bits
(a conservative bound compatible with the ingress integer-length limit).

Object schemas declare `properties`, a tuple of `required` names, and an explicit
`additional_properties` flag or schema (default false). Arrays require one `items` schema. Numeric schemas
support inclusive `minimum` and `maximum`; strings and arrays support `min_length` and `max_length`.
Scalar `enum` values must have the declared type. A `number` enum compares integer and float values
by numeric equality; other scalar enums also require the exact JSON type.

Schema configuration is immutable and property mappings are copied. Schemas reject wrong option
types, inapplicable constraints, reversed bounds, undeclared/duplicate required names, more than
256 object properties or enum members, enum strings longer than 2048 characters, more than 32 levels,
more than 4096 expanded nodes, or more than one million characters in expanded names and enum values.
Unions use 2–16 `any_of` alternatives; `nullable()` adds a null alternative. A successful alternative
accepts the value; failed branch errors collapse to `schema_any_of`. Candidate work shares one budget.
The [runtime schema/type API](runtime-schema-types.md) adds strict supported-keyword import/export
and trusted annotation compilation. Imported integer schemas use JSON Schema's mathematical integer
semantics and accept integral floats; direct Python schemas remain strict by default.
`json_schema()` exports a portable projection; `export_output_schema()` preserves the Python integer
distinction using a documented extension. Neither encodes runtime resource budgets.

`schema.validate(value)` returns specific `ValidationIssue` paths such as `$["scores"][2]`.
Errors preserve input object order; missing required fields follow declared required order.
At the configured error cap, one additional `schema_issue_limit` issue explicitly reports truncation.

## Semantic execution

Implement `OutputValidator.check(value, context) -> RuleResult`, then attach instances with
`RuleBinding(rule_id, path, validator, on_fail)`. The context contains a rule ID, an exact tuple path,
and the current phase. For example, `("predictions", 0, "label")` names a nested array item.
The empty tuple targets the root. Paths are resolved in the current document before each binding;
an absent optional path or currently absent array position is skipped. Pipeline construction first
checks paths against the schema: undeclared closed-object keys, descendants of scalars, wrong
object-key/array-index types, and indices excluded by a declared maximum array length are
configuration errors. This prevents misspelled bindings from silently bypassing validation.
An undeclared member of an object with `additional_properties=True` has unknown shape, so syntactically
valid paths below it are allowed and skip when the actual shape does not contain the target. Declared
properties remain checked even on an open object. Typed additional properties are path-checked,
and a union path must be possible in at least one alternative. There is no wildcard selection.

Return `RuleResult(True)` to accept. Return `RuleResult(False, code, message)` to fail, optionally
providing a fourth `fix` argument. Failures require lowercase stable identifiers and bounded messages.
`TrimmedString` suggests removal of leading/trailing whitespace. `StringChoices` checks exact
membership and optionally suggests a case correction only when there is one unambiguous match.

The deterministic phases are:

1. Snapshot the input and validate the schema. Structural failures prevent callback execution.
2. Execute bindings in their declared order on isolated copies. `reject` records a failure.
   `fix` accepts one JSON-valid proposal only after the same callback accepts that proposal.
   `filter` removes the named object member and later bindings on the absent path are skipped.
3. After changes, check the complete document's resource bounds and schema. If still valid,
   verify every present rule again on the final document with no further repair. A later fix
   cannot silently invalidate an earlier rule.

Filtering is limited to object members. Removing a required member causes schema failure. Arrays
can be replaced through an explicit fix, but filtering never implicitly shifts array indices.
Fixes are semantic repairs, so they cannot rescue an initially schema-invalid document. Callers
that want coercion must make that separate transformation explicit before validation.

The pipeline catches callback exceptions and malformed return values as `validator_contract` without
copying exception messages into diagnostics. Invalid proposed JSON becomes `invalid_fix`; aggregate
repair growth becomes `output_budget`. A callback can run in `initial`, `repair`, and `final` phases,
so validators must be deterministic and should not depend on side effects or call count.

## Result and privacy contract

`OutputReport.valid` is true only after all applicable checks pass. `output` returns a fresh JSON
copy on success and `None` on failure. A valid JSON-null output also returns `None`, so inspect
`valid` rather than treating `None` as the validity predicate. Mutating input, callback values, or
a previously returned output does not change the report. Per-field `outcomes` retain rule, path,
phase, status, and diagnostic code/message, and `invocations` counts actual callback calls.

`report.to_dict()` omits the output unless `include_output=True`. Callback-provided messages and
object keys may contain sensitive data; this is not an automatic logging redactor. No API sends
these records to a telemetry service.

## Resource and execution boundaries

| Budget | Default | Hard ceiling |
|---|---:|---:|
| JSON depth | 32 | 64 |
| JSON value nodes | 10,000 | 100,000 |
| Total characters in strings and object keys | 1,000,000 | 8,000,000 |
| Stored validation issues | 100 | 1,000 |
| Callback invocations, all phases combined | 1,000 | 10,000 |
| Schema/value steps, shared across union branches per validation pass | 100,000 | 1,000,000 |
| Pipeline bindings | 256 | 256 |

Each snapshot accepts only exact built-in JSON scalar/list/dict types. It rejects cycles, custom
containers, non-string keys, isolated Unicode surrogates, nonfinite numbers, and oversized integers.
Shared acyclic containers are independently copied. Limits apply before callbacks, to fixes, after
every replacement, and to the final document. Report outcomes are bounded by calls plus one budget
failure. There are no unbounded automatic retries.

`validate_json_bytes()` uses the existing strict decoder and defaults to a 4,000,000-byte limit.
Duplicate keys, nonstandard numbers, invalid UTF-8, and oversized raw input retain the existing
`PayloadValidationError` contract. Invalid Python JSON values and snapshot-budget exhaustion raise
`OutputContractError`. Ordinary schema/rule failures return a rejected report.

Validators are trusted Python code. Copies protect the pipeline's data from incidental callback
mutation, but synchronous calls cannot sandbox Python, restrict callback network access, bound a
callback's own allocation, or interrupt its execution. Async callbacks are rejected. There is no
LLM invocation, re-ask loop, server, distributed validator executor, or structured-token streaming
implementation in this API.
