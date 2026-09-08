# Explicit dataclass adaptation

`DataclassAdapter[T]` validates bounded JSON, invokes real trusted Python constructors, and returns
an actual `T`. It is separate from `AnnotationAdapter`, whose return contract remains isolated JSON.
Both use the same annotation compiler, immutable `OutputSchema`, strict ingress and JSON encoder.
There are no runtime dependencies, provider calls or schema-directed imports.

Twelve [strict standard-library scalar fields](typed-scalar-fields.md) also compose with these
models: temporal types, Decimal, UUID, and IPv4/IPv6 addresses, networks and interfaces.

Run the offline example with `python examples/dataclass_adaptation.py`.

```python
from dataclasses import dataclass, field
from typing import Annotated
from payload_palette import DataclassAdapter, FieldConstraints


@dataclass(frozen=True, slots=True)
class Detection:
    label: str
    score: Annotated[float, FieldConstraints(minimum=0, maximum=1)]


@dataclass(kw_only=True)
class Result:
    detections: list[Detection]
    tags: list[str] = field(default_factory=list)


adapter = DataclassAdapter(Result)
result = adapter.validate_json_bytes(b'{"detections":[{"label":"bicycle","score":0.8}]}')
assert type(result.detections[0]) is Detection
assert adapter.dump_python(result)["tags"] == []
encoded = adapter.dump_json(result)
```

## Input, construction and output are distinct

The root must be a concrete stdlib dataclass **type**, not an instance, dictionary, generic alias
or arbitrary class. Input is exact built-in JSON data; supplying already constructed dataclasses to
`validate_python` is an error. `dump_python` requires the exact declared dataclass types, recursively;
shape-compatible dictionaries and subclasses are not accepted in their place. The returned JSON
graph is fresh, and dumping neither invokes factories nor reconstructs objects.

Supported fields compose nested dataclasses with the existing primitives, homogeneous lists,
string-key maps, `TypedDict`, scalar `Literal`, nullable/union types and `Annotated[T,
FieldConstraints(...)]`. Inherited, frozen, slotted and keyword-only dataclasses are supported.
Live `ClassVar` fields are not wire fields. A nullable field is still required unless it has a
literal default or factory. Integers remain strict Python ints; `float` denotes the existing JSON
number family and does not turn an accepted integer into a float.

`input_schema` is closed and permits omission only where the dataclass declares a default or
factory. `output_schema` requires every declared dataclass field after construction. Optional
`TypedDict` keys stay optional. Neither schema embeds executable factories or constructors.
Portable JSON Schema projections do not encode constructor behavior, all budgets or the strict
Python integer distinction; see [schema interchange](runtime-schema-types.md).

Validation proceeds in this order:

1. Snapshot and structurally validate **all supplied JSON**, including all supplied nested fields.
2. Resolve every supplied union involving construction without calling a factory or constructor.
3. Prepare and validate omitted defaults throughout the complete tree.
4. Construct nested values and call each declared class with all completed fields as keywords.
5. Project and revalidate the complete final object graph, including mutations by `__post_init__`.

No adapter constructor runs before every default has been prepared. A factory may itself call
constructors; that is trusted code inside the factory, not a speculative adapter branch. The
guarantee is **final graph validation**, not validated intermediate arguments to every parent:
a child's post-init can temporarily invalidate a field and the parent's post-init can repair it.
If the final graph remains invalid, no model is returned as accepted.

## Defaults and ownership

Literal defaults are typed-projected and validated when the adapter is created, then retained as
immutable encoded JSON, not as the caller's live default object. This invokes no adapter factory
or constructor. Mutating a literal's source list later cannot alter the captured default.

For each omitted factory field, the captured synchronous factory runs once. Its return must have
the actual declared types: a dataclass field requires that dataclass, not a dict with the same
shape. The result is typed-projected to fresh JSON, validated and reconstructed with real class
constructors. A factory-created nested dataclass therefore may run its constructor once inside
the factory and again during adapter reconstruction. The same reconstruction applies to nested
literal dataclasses. There is no `object.__new__` bypass, `deepcopy`, pickle or `dataclasses.asdict`.

The adapter isolates JSON input and projected defaults. It cannot guarantee that arbitrary trusted
`__new__`, metaclass, constructor or factory code will not return a previously retained instance,
expose a reference, mutate other objects or perform external side effects. Side effects cannot be
rolled back if a later default, constructor or final check fails. Returned models retain the
class's mutability; no assignment validation is installed. Dumping rechecks their current values.

## Unions do not guess a constructing branch

A union containing a dataclass or typed scalar anywhere must have exactly one matching input
branch. Scalar-bearing branches add their pure semantic preflight to the structural schema check;
branches without scalar conversion keep the existing structural rule. This is checked before
callbacks, never by trying constructors and keeping the
first one that succeeds. Distinct required `Literal` tag fields are a useful way to make shapes
unambiguous, but there is no separate discriminator engine. Pure JSON unions retain existing
any-of behavior, such as a Python int satisfying both `int` and `float` annotations.

The same input-shape uniqueness is required for dumping. For example, a typed `A(value=1)` cannot
be dumped through `A | B` if `B` accepts `{value: 1}` with extra defaulted fields: its bytes could
not select the original type on reloading, even though the runtime object is visibly an `A`.
Ambiguity is a structured `dataclass_union_ambiguous` error, not hidden type coercion.

## Trusted code and rejected definitions

Use live annotations as above. Strings, unresolved `ForwardRef`, recursive or generic models,
`InitVar`, `init=False` classes/fields, custom field descriptors, nonempty field metadata, aliases,
custom serializers, arbitrary validators, untyped containers, `Any`, tuples/sets, bytes and standard
types outside the [explicit scalar table](typed-scalar-fields.md) are not implemented.
Custom constructor signatures must accept all declared
fields as keywords. Known asynchronous factories, constructors and post-init functions are
rejected. Class metadata and signatures are snapshotted for the plan, but the class is still a
trusted Python object: metadata inspection or later class/descriptor mutation is not sandboxed.

Ordinary factory/constructor failures become stable structured errors without their raw exception
prose. `KeyboardInterrupt`, `SystemExit`, cancellation and other control exceptions propagate.
At a direct callback-return boundary, an invalid **fresh native coroutine** is closed without
running its body. Borrowed tasks, futures, started coroutines and custom awaitables are rejected
without cancellation, advancement or closing. The adapter does not discover or own hidden resources
created inside a trusted callback or embedded inside its arbitrary invalid return graph. Their
creator must manage them. In particular, do not use a synchronous `__post_init__` wrapper that
returns a coroutine: stdlib's generated `__init__` discards that return before the adapter can
observe it, potentially leaking an unawaited coroutine even when the fields are valid. This is an
unsupported callback contract; the adapter does not rewrite the class to intercept it. No async
adaptation API is claimed.

## Limits

`SchemaDefinitionLimits` bounds the same shared compiler as `AnnotationAdapter`. `OutputLimits`
bounds each JSON/projected/prepared graph's depth, nodes and characters. Schema evaluation uses one
monotonic `max_schema_steps` counter across the supplied input, union candidates, defaults and final
checks in each adapter operation. Failed union candidates do not reset it.

`DataclassLimits` adds aggregate adapter traversal work (`max_steps=100_000`, ceiling 1,000,000),
direct adapter callback entries (`max_callbacks=1_000`, ceiling 10,000), and total compiled literal
JSON storage (`max_default_bytes=4_000_000`, ceiling 32,000,000). Values are positive exact ints.
Callback admission also respects `OutputLimits.max_invocations`. Compilation/default capture and
each validation/dump operation have separate counters; within an operation they never reset.
`JSONSerializationOptions` separately caps exact returned UTF-8 bytes.

These are explicit data/work bounds, not a process-RSS or wall-clock sandbox. Bounded input,
prepared state, constructed model and final projection can coexist. A trusted callback can allocate
or block before returning; no timeout, subprocess isolation or arbitrary Python preemption is
provided. This original supported subset is not complete Pydantic/dataclass ecosystem equivalence.
