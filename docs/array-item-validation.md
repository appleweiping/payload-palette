# Per-item semantic validation for homogeneous arrays

`ArrayRuleBinding` applies one synchronous semantic validator to each present
item of a schema-declared homogeneous JSON array. It complements exact-index
`RuleBinding`; it does not change the original binding's path semantics.

```python
from payload_palette import (
    ArrayRuleBinding,
    OutputSchema,
    TrimmedString,
    ValidationPipeline,
)

item = OutputSchema("object", properties={"label": OutputSchema("string")})
schema = OutputSchema("object", properties={"predictions": OutputSchema("array", items=item)})
pipeline = ValidationPipeline(
    schema,
    (ArrayRuleBinding("trim_labels", ("predictions",), ("label",), TrimmedString(), "fix"),),
)
report = pipeline.validate({"predictions": [{"label": " yes "}, {"label": "no"}]})
assert report.valid
assert report.output == {"predictions": [{"label": "yes"}, {"label": "no"}]}
```

`array_path` names a declared homogeneous array; `()` selects a root array.
`item_path` is relative to each element; `()` checks the whole element. A
callback receives a concrete path such as `("predictions", 0, "label")`, not
a wildcard. Empty arrays and absent optional arrays or item fields cause no
callback. Items are visited in increasing index order after earlier rule
bindings finish. Every fix is copied, rechecked and applied to one concrete
target; `filter` may remove a named object member but never an array element.
After any change, the complete schema and every present rule target are
verified again without further repair. A rejected report exposes no accepted
output, even if earlier elements were privately repaired.

The `max_items` default is 1,000 and its hard ceiling is 10,000. An array
exceeding that binding's limit produces a fixed `validator_fanout` issue at
its declared array path before any callback from that binding; no subset is
silently processed. All exact and per-item callbacks share the existing
`OutputLimits.max_invocations` budget, including repair and final phases. The
first over-budget target is not invoked. JSON node/depth/character, issue and
schema-work limits remain in force. Trusted callback time, allocation and
external side effects are not sandboxed or rolled back.

Only declared object properties and concrete indices may lead to the selected
array. The selected array must use homogeneous `items` with no positional
`prefix_items`. Dynamic object keys, combinations on the path to the array,
nested selectors, wildcard syntax and general JSONPath are not supported.
Within an item, the suffix must be possible under its schema. Concrete numeric
indices and declared field names may appear in outcomes and generation
feedback; callback-provided messages can still contain sensitive prose, as
with existing rules. Accepted output is omitted from `report.to_dict()` unless
explicitly requested.

An `AsyncValidationPipeline` can compose this synchronous stage with its
existing exact-path asynchronous checks; complete-output generation and final
incremental validation reuse the same pipeline. The v1 declarative output
configuration and CLI do **not** serialize this Python-only binding and reject
it during export. Native call validation and schema import/export are unchanged.
See [`examples/array_item_validation.py`](../examples/array_item_validation.py)
for an offline example. This bounded profile is not Pydantic or Guardrails
API compatibility.
