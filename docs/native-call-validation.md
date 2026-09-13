# Explicit native call validation

`CallAdapter` binds a real Python call, checks existing native arguments, invokes
the chosen target once, and optionally checks its return. It does not decode JSON,
coerce strings, reconstruct dataclasses, or replace objects with their JSON
projections. Run `python examples/native_call_validation.py` for an offline
sync/async example with identity checks and observable failure boundaries.

```python
from decimal import Decimal

from payload_palette import CallAdapter


def quote(amount, /, count=1, *, label="sample"):
    return amount


checked = CallAdapter(
    quote,
    annotations={"amount": Decimal, "count": int, "label": str, "return": Decimal},
    validate_return=True,
)
amount = Decimal("12.3400")
assert checked.call(amount, count=2) is amount
```

## An explicit annotation map, never hidden evaluation

`annotations` is a required exact built-in dict naming every exposed parameter.
`"return"` is optional unless `validate_return=True`; if supplied, it is compiled
even when return checking is disabled. Missing/extra keys, Any, strings,
ForwardRefs, unsupported metadata and recursive/generic models are rejected.
The map is compiled once; subsequent map mutation cannot change the plan.

The target's source annotations are not read at all. This also supports functions
in modules using postponed annotations without evaluating their strings. No
`get_type_hints`, annotationlib retrieval, target `inspect.signature`, eval,
namespace resolution, source inspection, or annotation-directed imports occur.
In Python 3.14, even `inspect.signature(..., eval_str=False)` can execute deferred
annotations. Capturing the target's native code/default slots avoids that path.

Nested types are trusted Python definitions, not arbitrary sandboxed objects.
Native dataclass checks use existing materialized Field.type metadata and skip
constructor-signature inspection and factory/default preparation. No constructor
or factory is invoked by this adapter, including when validating function defaults.
The [construction adapter](dataclass-adaptation.md) keeps its separate existing
trusted-constructor contract unchanged.

TypedDict requires an already explicitly materialized annotation dict. On Python
3.14 a normal deferred class or functional TypedDict still has an annotation
function and is rejected. A trusted caller may supply its map explicitly before
compilation, for example `Record.__annotations__ = {"value": int}`. The adapter
checks native annotation storage without invoking that function or mutating the
type. Required/optional metadata must still match the declared fields; include
the appropriate Required/NotRequired wrappers in the explicit map. No fallback
to lazy annotation evaluation is provided.

## Real binding and native values

All five parameter kinds are supported: positional-only, positional-or-keyword,
keyword-only, `*args`, and `**kwargs`. The annotations for variadic parameters
apply to each surplus value, not to a prepacked tuple/dict. Surplus keyword order
is preserved. With a varkw parameter, a positional-only parameter's name can also
appear as an unrelated surplus keyword; it never fills the positional slot.

Accept exact Python functions and ordinary already bound Python methods. A bound
receiver is retained as an opaque trusted object and excluded from the annotation
map and resource accounting. An unbound function has no receiver exemption.
Resolved static methods and class methods follow these same rules. Partials,
built-ins, arbitrary callable objects, descriptor objects, generators, async
generators and exotic receiver-through-varargs methods are unsupported.

The adapter snapshots real code shape and default references, ignoring forged
`__signature__`, `__wrapped__`, and coroutine markers. `adapter.signature` is an
immutable introspection view, not a serializable call contract. Replacing the
target code after compilation is rejected before invocation. Target globals,
closures, trusted class behavior and external side effects are not frozen.

Accepted native values compose the existing strict primitive/list/dict schemas,
fixed/variadic/empty tuples, materialized TypedDict, Literal/Annotated constraints,
concrete dataclasses and the twelve [typed scalar types](typed-scalar-fields.md).
Integers exclude bool and retain the existing 850-bit ceiling. Float annotations
also accept finite ints without conversion. Tuple annotations require tuples,
not JSON lists. Dataclasses and scalar instances require their exact declared
types, not subclasses or wire-shaped substitutes. Cycles, invalid Unicode and
unsupported objects fail. No extra scalar codec or validator registry is added.

Existing typed union round-trip rules are preserved: a native date through
`date | str` is ambiguous because its projection also satisfies str. This is
deliberately stricter than selecting whichever runtime isinstance check succeeds.

## Defaults and ownership

Every captured fixed default is checked at compilation, and each omitted default
is checked again within the complete bound graph at call time. Defaults are
**retained references**, unlike the construction adapter's frozen wire defaults.
Mutating a shared default can alter subsequent results or cause later validation
failure. Reassigning the target's default containers does not replace captured
defaults: the adapter explicitly supplies every completed fixed parameter.

Arguments, aliases and validated returns retain their original native identity.
Temporary JSON projections are only for validation. Callers must not concurrently
mutate borrowed graphs; this is not a thread-safe live-object snapshot. The target
can mutate arguments, and there is no post-call argument recheck or rollback.
A bad return is rejected only after the target has already performed its work.

`validate_return=False` is a real bypass: arbitrary return objects, resource
handles and awaitables are passed through untouched and become the caller's
responsibility. No size, type, cleanup or JSON guarantee applies to that result.
With checking enabled, an invalid fresh unstarted native coroutine directly
returned by the target is closed without executing its body. Borrowed Tasks,
Futures, started coroutines, generators and custom awaitables are never cancelled,
advanced or closed to make them fit. Hidden resources inside invalid graphs are
not searched for or owned.

## Async and errors

Use `.call` only for a sync function, and `await .call_async(...)` only for a native
coroutine function. A sync function merely marked as coroutine-like remains sync.
Async calls are lazy until awaited. An initial cooperative checkpoint delivers
already requested cancellation before argument validation or target creation;
a previously caught cancellation count alone does not prohibit a new call.

The target coroutine is directly awaited in the caller task. No child task,
background scheduler, executor, retry, timeout or event loop is created. Normal
cancellation propagates; if the target suppresses cancellation and returns, its
result is handled normally. Nested awaitable results are not automatically awaited.
Each call has independent budgets, not an adapter-global concurrency/recursion cap.

Invalid definitions use `SchemaDefinitionError`; binding failures use one stable
`call_bind` issue at `$["arguments"]`. Native/schema issues preserve existing codes
with parameter/index/key paths or `$["return"]`. Work/size/admission violations
use `OutputContractError`. Diagnostics do not contain argument values, callable
reprs or raw binding-exception prose. Schema issue counts remain bounded, while
native traversal fails fast rather than collecting every parameter problem.

Exceptions raised by the target body itself propagate unchanged, including
TypeError, cancellation, MemoryError and control exceptions. A target TypeError
is not a binding error. Raw target exceptions/tracebacks are not redacted, and
trusted target or descriptor side effects are not reversible.

## Resource bounds

`CallLimits` requires positive exact ints, excluding bool:

| Limit | Default | Ceiling |
| --- | ---: | ---: |
| `max_parameters` | 64 | 256 |
| `max_arguments` | 1,024 | 100,000 |
| `max_keyword_characters` | 16,384 | 1,000,000 |
| `max_steps` | 100,000 | 1,000,000 |

Raw argument count and completed count after default insertion must each fit.
Surplus values count individually. Keywords require exact built-in strings,
admitted before length/Unicode inspection; subclasses are rejected without their
length hooks. Each keyword is also at most 256 Unicode scalar characters.
Python has already evaluated arguments and unpacking and
allocated the incoming argument containers before adapter admission.

Existing `SchemaDefinitionLimits` cover one combined argument/return definition
forest, including generated record and variadic wrapper nodes. Definition budgets
do not reset per parameter. All fixed defaults share a separate compilation-time
projection/work budget; no default-byte cache or factory execution is introduced.

At runtime, the complete arguments record and any validated return share
`OutputLimits.max_nodes` and `max_characters`; record/key/container overhead and
repeated alias occurrences count. Return depth starts at zero, but its size uses
the remaining allowance after arguments. All binder/native traversal work shares
`max_steps`; schema and failed union probes share `max_schema_steps`. Existing
temporary union projections are individually bounded and consume shared work.
One explicit target entry is charged to `max_invocations`. Computable admission
and argument-container allocation finish before that entry; an oversized return
can still fail afterwards because its size was not known beforehand.

These are data/work limits, not total RSS, elapsed-time, recursive-call, borrowed
receiver or trusted callback allocation bounds. Existing scalar introspection
limitations, such as an already huge Decimal coefficient, still apply.

## Verification and remaining scope

The independent binding oracle uses handwritten functions, stdlib bind and actual
function calls. On tested CPython 3.14.5, one defaulted positional-only vector
was accepted by inspect.bind but rejected by the real function; real Python call
behavior is the acceptance authority, and that discrepancy is retained explicitly.
Native identity/defaults, deferred annotation non-execution, scalar composition,
aggregate boundaries and direct-async ownership have focused regression tests.
Full-suite/platform/package/hosted evidence is recorded separately when run.

This is not a Pydantic decorator compatibility layer. Decorators, annotation
inference/evaluation, Any/coercion, partials, aliases/Field/Unpack, broader types,
custom validators, JSON-to-call/config/CLI transport, scheduling and hard callback
preemption remain open. See the [whole-reference inventory](parity-validation.md).
