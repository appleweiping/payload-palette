# Explicitly trusted native-call decorator

`native_validated_call` compiles a function's declared annotations into one
strict `CallAdapter` plan. It checks existing Python values before calling the
function, never coerces them, and optionally validates the result. This is an
opt-in declaration workflow, not Pydantic `validate_call` compatibility.

```python
from decimal import Decimal
from payload_palette import native_validated_call


@native_validated_call(trust_annotations=True, validate_return=True)
def quote(amount: Decimal, /, count: int = 1) -> Decimal:
    return amount * count


assert quote(Decimal("12.50"), 2) == Decimal("25.00")
```

The exact boolean `trust_annotations=True` is required **before** any source
annotation is read. Python 3.11–3.13 normally materialize annotations when a
function is defined. Python 3.14 may evaluate them when this decorator reads
them; trusted annotation expressions can perform arbitrary side effects or
raise. This code is not sandboxed, timed out or resource-preempted. Malformed
limit/configuration types are rejected before reading annotations, but valid
configuration does not make annotation evaluation safe. The decorator does not
evaluate string annotations, import referenced names or silently infer `Any`.
`from __future__ import annotations`, `ForwardRef`, `Any`,
unsupported types and missing parameter annotations are rejected. Return
annotation is required when `validate_return=True`.

Only exact native Python functions are accepted. A synchronous function gets a
synchronous wrapper; `async def` gets a real coroutine-function wrapper whose
validation and target body remain lazy until awaited. All five Python
parameter kinds use the existing native binder. The compiled annotation map
is a snapshot: changing a function's annotation dict later does not alter
the plan. Code replacement is still detected by `CallAdapter`.

The wrapper copies a few identity fields and publishes the compiled
`__signature__`, but deliberately does not use `functools.wraps` or copy the
source's `__annotations__`, `__annotate__` or dictionary. On Python 3.14 those
copies can cause an additional deferred-annotation evaluation. The wrapper's
own `__annotations__` is empty; use its compiled signature for introspection.
No descriptor/method decoration, stacked-wrapper transparency or general
callable-object behavior is promised.

Invalid arguments never enter the target. A target exception propagates
unchanged. A rejected return occurs **after** the target ran; no side effects
are rolled back or retried. The existing `OutputLimits`,
`SchemaDefinitionLimits` and `CallLimits` can be passed through and cover
compilation/validation work, not trusted annotation code or target execution.
Errors follow the same structured codes and paths as `CallAdapter` and do not
include raw argument values in newly added diagnostics.

The direct form is also available:

```python
def identity(amount: Decimal) -> Decimal:
    return amount


checked = native_validated_call(identity, trust_annotations=True)
```

Run `python examples/native_validated_call.py` for an offline identity and
async example. Pydantic-style coercion, unannotated `Any`, `Field`, aliases,
`Unpack`, partials, callable instances, class/method descriptors, generic
models and Guardrails validator/re-ask integrations remain unsupported.
