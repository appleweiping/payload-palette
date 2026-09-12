# Declarative output pipeline configuration

`load_output_config` compiles a closed JSON configuration into the existing immutable
`ValidationPipeline`. It does not introduce another validation engine, call a validator during
configuration, import plugins, evaluate text, resolve references, or contact a model or network.
The [output CLI](output-cli.md) uses this same compiler and provides bounded final reports with
exclusive file publication. Existing request `validate` and `normalize` commands are unchanged.

```python
from payload_palette import load_output_config, export_output_config, output_config_digest

pipeline = load_output_config(
    {
        "kind": "payload-output-config",
        "version": 1,
        "schema": {"type": "string"},
        "rules": [
            {
                "id": "trim",
                "path": [],
                "validator": {"kind": "trimmed_string"},
                "on_fail": "fix",
            },
            {
                "id": "choice",
                "path": [],
                "validator": {
                    "kind": "string_choices",
                    "choices": ["A", "B"],
                    "fix_case": True,
                },
                "on_fail": "fix",
            },
        ],
    }
)
report = pipeline.validate_json_bytes(b'" a "')
assert report.valid and report.output == "A"
normalized = export_output_config(pipeline)
identity = output_config_digest(pipeline)
assert len(identity) == 64
```

## Closed v1 data format

The root requires exact `kind: "payload-output-config"`, exact integer `version: 1`, and `schema`.
Unknown fields fail at every level; booleans, floating-point versions, coercions, and aliases are
not accepted. The only optional root fields are `limits` and `rules`. Omitted limits mean existing
`OutputLimits()` defaults; omitted rules mean an empty ordered list. Each of the six output limit
fields may be omitted independently. Their bounds and semantics remain those documented in the
[output contract](output-validation.md#resource-and-execution-boundaries).

Schemas use the existing [supported schema interchange](runtime-schema-types.md), including its
independent definition limits. Imported objects are open by default. Direct Python schemas are
closed by default; normalized exports state the choice explicitly. Payload's strict-integer
extension is retained. Arbitrary JSON Schema keywords, references, Python annotations and class
names are not a configuration language here.

Rules require `id`, `path`, and `validator`; `on_fail` defaults to `reject`. Identifiers match
`[a-z][a-z0-9_]{0,63}` and must be unique. Paths are arrays of at most 32 exact string keys or integer
indices: keys contain at most 256 Unicode scalar characters; indices range from 0 through 99,999.
An empty path selects the root. No wildcard or string-path parser is used. Impossible schema paths
fail during compilation; possible absent optional/alternative paths skip during validation.

Only these exact built-ins have a data representation:

| Validator | Fields besides required `kind` |
|---|---|
| `trimmed_string` | None |
| `string_choices` | Required `choices`: 1–256 unique exact strings, each at most 2048 characters; optional exact boolean `fix_case`, default false |

Actions are exactly `reject`, `fix`, or `filter`. Filtering is limited to object-member paths and
does not waive required fields. The existing initial schema check, one-attempt repair and immediate
recheck, complete final schema check, ordered final semantic verification, and invocation accounting
are unchanged. Built-ins do not add custom callback text. Reports still have the existing privacy
boundary: output is omitted by default, but diagnostic paths and rule identifiers are data-bearing
metadata, not automatically redacted secrets.

## Admission and ownership

`OutputConfigLimits` sets independent, lowerable hard ceilings:

| Field | Default and hard ceiling | Minimum |
|---|---:|---:|
| `max_input_bytes` | 4,000,000 | 1 |
| `max_depth` | 96 | 0 |
| `max_nodes` | 100,000 | 1 |
| `max_characters` | 1,000,000 | 1 |
| `max_rules` | 256 | 0 |
| `max_path_segments` | 8,192 | 0 |
| `max_choice_values` | 4,096 | 0 |

Root depth is zero. Nodes count every container and scalar value, not object keys. Character counts
include every key and string value. Paths and choices are aggregated across all rules. Shared
acyclic containers are copied and charged for every occurrence; active cycles fail. Only exact
built-in JSON containers and finite scalar types are admitted, without conversion/copy/equality
hooks on foreign objects. Integers and Unicode follow the existing output scalar restrictions.
The configuration depth ceiling is independent of output depth: a 32-level schema can need more
than 64 JSON-container levels inside this envelope.

`load_output_config_json_bytes(payload, *, limits=None)` uses strict UTF-8 JSON ingress, rejecting
duplicates, nonstandard/nonfinite numbers and isolated surrogates. `max_input_bytes` applies before
copying/decoding raw bytes. The object loader instead admits the in-memory graph. Both loaders
also require the normalized export to fit **all default** graph and canonical-byte ceilings before
returning a pipeline. Thus UTF-8 that expands excessively under ASCII escaping, or a terse graph
whose inserted defaults exceed the node cap, cannot create an unexportable default configuration.

`export_output_config(pipeline, *, limits=None)` admits the normalized graph and its canonical
ASCII bytes under the supplied limits. Arbitrarily lowered policies are representation-specific:
a short input can fit a lowered raw-byte/node cap while the explicit export does not. Default
accepted configurations always export and reload under default limits, including byte interchange.

Loaded schema mappings, rules, paths, choices and limits are owned immutable configuration. Mutating
source dictionaries or exported dictionaries cannot change a compiled pipeline or later export.
Exports accept only exact synchronous `ValidationPipeline` and exact `TrimmedString`/`StringChoices`
validators, not subclasses, asynchronous pipelines, or custom validators. Unsupported validator
objects are refused without calling their attributes. Live Python pipeline objects remain trusted
configuration; bypassing frozen-object invariants or concurrently modifying internals is outside
the contract. These APIs are not a Python sandbox or hard CPU/RSS limit.

## Normalization, identity and errors

Export fills defaults, includes the schema dialect, and preserves rule, choice, required-field,
and union-branch order. Each export returns a fresh graph. `output_config_digest` returns lowercase
SHA-256 hex over that graph's sorted-key, compact, finite JSON with `ensure_ascii=True`, encoded as
ASCII without a newline. It hashes bounded chunks without constructing another complete byte copy.
Object key order and omitted defaults do not affect identity. Ordered lists do; numeric forms such
as `1` and `1.0` need not share identity. This is syntax-normalized configuration identity, not a
proof of semantic equivalence, validation truth, provenance, or publisher authentication.

Invalid data raises `OutputConfigError`, a `PayloadValidationError` subclass, with stable codes:
`config_type`, `config_cycle`, `config_budget`, `config_keyword`, `config_required`, `config_kind`,
`config_version`, `config_constraint`, `config_validator`, `config_duplicate_rule`, and
`config_path`. Nested schema errors retain their `schema_definition_*` code with a path prefixed
by `$["schema"]`. Invalid limit objects/options raise `ValueError`. Byte decoding failures retain
the existing `PayloadValidationError` codes and messages; they are not a CLI-safe redaction layer.
Genuine control exceptions propagate. No configuration failure runs a validation callback.

`tests/test_output_config.py` contains handwritten admission and report oracles, direct-pipeline
comparisons, exact boundaries, normalization expansion, ownership, hostile-object refusal and
strict byte-ingress regressions. Full repository, package and hosted acceptance remain separate;
the [whole-reference assessment](parity-validation.md) remains open.
