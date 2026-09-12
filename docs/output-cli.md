# Declarative output validation CLI

These offline commands compile the [closed v1 configuration](output-configuration.md) and run the
existing complete-output pipeline. They do not call models, import validators, fetch references,
discover configuration, read ambient credentials, or send telemetry. The request `validate` and
`normalize` commands keep their existing options, reports, publication behavior and exit codes.

## Commands and limits

```bash
payload-palette check-output-config CONFIG.json
payload-palette validate-output CONFIG.json MODEL-OUTPUT.json
payload-palette validate-output CONFIG.json - --include-output -o NEW-REPORT.json
python -m payload_palette validate-output CONFIG.json MODEL-OUTPUT.json
python examples/validate_configured_output.py
```

The final example creates and removes its own temporary directory, checks a configuration,
repairs a candidate, and proves that a second invocation cannot replace the report. It needs
neither credentials nor a network. Each exact command accepts `--help` and `--version` without
opening files. Put the command first. Abbreviated options are rejected.

`CONFIG.json` is an explicit UTF-8 JSON file, never `-`. `validate-output` additionally requires
a complete UTF-8 JSON input file or `-` for binary standard input. A JSON string must include
its JSON quotes. Empty input is invalid. Caller-owned streams stay open; owned input files are
closed before validation. Binary short reads are handled in bounded chunks, reading at most one
byte beyond the cap to detect excess. There is no stdin timeout or hard CPU/RSS guarantee.

| Option | Commands | Default | Hard ceiling |
|---|---|---:|---:|
| `-o PATH`, `--output PATH` | Both | `-` (stdout) | New file only |
| `--max-config-bytes N` | Both | 4,000,000 | 4,000,000 |
| `--max-input-bytes N` | `validate-output` | 4,000,000 | 32,000,000 |
| `--max-report-bytes N` | Both | 4,000,000 | 32,000,000 |
| `--include-output` | `validate-output` | Output omitted | Accepted output only |

`N` uses positive ASCII decimal digits without signs, leading zeroes, whitespace or exponents.
The report byte limit includes its final LF. Config graph/canonical-export limits and the
configuration's output-work limits still apply independently; increasing a raw-input cap does not
relax them. A report that exceeds its cap fails before creating any report temporary file.

## Reports, exits and privacy

Reports are one compact sorted-key JSON document encoded as ASCII with Unicode escapes, followed
by exactly one LF. Successful file publication is silent on stdout and stderr. Otherwise the
chosen report destination receives the final check or validation document:

- Check: `kind: "payload-output-config-check"`, `version: 1`, `valid: true`, `config_digest`, and
  `rule_count`. Checking compiles the configuration without executing validators.
- Validation: `kind: "payload-output-validation"`, `version: 1`, `config_digest`, and the existing
  `OutputReport` fields `valid`, `issues`, ordered `outcomes`, and `invocations`.
- With `--include-output`, the validation document also has `output`: a fresh accepted result, or
  `null` when rejected. Accepted JSON `null` also has `output: null`; use `valid` to distinguish it.

Configuration identity is SHA-256 of normalized configuration, not authenticity, provenance or
proof that a validation result is correct. Pipeline ordering, repairs, filtering, final checks and
invocation accounting are exactly the [existing output contract](output-validation.md).

| Exit | Meaning |
|---|---|
| `0` | Configuration or output accepted, and the requested final report completed; also help/version |
| `1` | Output rejected by strict decoding, input/value/work admission, schema or semantic rules; rejection report completed |
| `2` | Invalid arguments/configuration, input I/O, report serialization/admission, writing, publication or cleanup failure |

A report failure overrides an underlying acceptance/rejection status. Exit `1` is not used for
missing files or failed reads/closes. Syntax and admission failures produce a rejected validation
report with zero invocations and fixed messages: raw decoder text, duplicate key values and
malformed input are not included. Rejection reports do not expose partially repaired output.

Argument/configuration/I/O failures instead write a bounded JSON error notice to stderr:

```json
{"errors":[{"code":"invalid_arguments","message":"invalid output command arguments","path":"$"}],"kind":"payload-output-cli-error","publication":"none","stage":"arguments","valid":false,"version":1}
```

Error stages are `arguments`, `config`, `input` and `report`. Stable private error codes are
`invalid_arguments`, `config_read`, `invalid_config`, `input_read`, `report_limit`, `report_write`,
`report_cleanup`, `temp_changed` and `internal_error`. Their fixed prose excludes argument values,
filenames, configuration values and exception text/repr. A broken stderr may prevent the notice;
exit `2` remains authoritative. Malformed exact new-command arguments are sanitized; unrelated
legacy/root parser diagnostics keep their old behavior.

Output omission is **not comprehensive redaction**. Normal validation diagnostic paths may contain
input property names; rule paths and identifiers are configuration metadata. These can be sensitive.
The two allowed built-ins use fixed diagnostic prose, but their paths/IDs remain visible. Enabling
`--include-output` deliberately exposes accepted source/repaired values. Protect report destinations,
logs, argv visible to the operating system, and configuration files accordingly.

## Exclusive report publication and failures

The complete report is serialized and byte-admitted in memory first. For a file destination the
writer creates a unique same-directory temporary file, completes short writes, flushes, `fsync`s,
and closes it before publication. Windows uses `os.rename`; POSIX uses `os.link` followed by removal
of the temporary name. Both native operations refuse an existing destination. There is no replace,
copy/overwrite fallback, pre-delete, or existence-check-based publication decision. A destination
that is already a report, input/configuration file, directory or symlink is not overwritten.
Competing writers can have only one winner at the same destination name.

The private error notice reports what the writer knows:

| `publication` | Meaning |
|---|---|
| `none` | No native final-file publication or stdout report write was attempted |
| `unknown` | A native publication/stdout write was attempted but did not acknowledge completion; a complete file or partial stdout may exist |
| `complete` | Native file publication acknowledged completion, but subsequent owned-temp cleanup failed |

For example, even a destination collision conservatively reports `unknown`; the CLI never deletes
or rolls back the final destination to resolve an uncertain result. Inspect an existing report
before deciding how to proceed. Do not treat any exit `2` as successful validation.

Cleanup checks the owned temporary file's regular-file device/inode identity. Missing names are
harmless; a foreign replacement is preserved. If identity cannot be established, the unverified
temporary name can remain rather than authorizing path-only deletion. A cleanup failure after
publication can leave both names pointing at a complete report. Genuine control exceptions
(`KeyboardInterrupt`, `SystemExit`, `GeneratorExit`, and groups containing controls) propagate after
owned cleanup. An original control is preserved even when cleanup also fails; a cleanup control
is not suppressed by an earlier ordinary error.

This is not a hostile-filesystem sandbox: trusted, stable directory ownership is required and
identity checks cannot eliminate every path race. Unsupported hard-link filesystems fail closed.
File `fsync` is not a claim of directory/power-loss durability. Stdout/pipes are not atomic and may
contain a prefix on write failure; caller streams are flushed but never closed.

## Evidence boundary

Focused tests include complete direct-pipeline report comparisons, private malformed input and
arguments, exact byte/LF limits, short I/O, serialization-before-temp admission, existing/aliased
targets, concurrent real processes, native Windows publication, and injected publication/close/
cleanup/control failures. Real symlink tests skip explicitly when Windows lacks creation privilege.
POSIX adapter and two-name hard-link tests on Windows are not a substitute for real Linux acceptance.
Full repository, package, Linux and hosted exact-head gates remain separate. The
[whole-reference objective](parity-validation.md) remains open; services, provider adapters and
arbitrary validator interchange are not implemented by this CLI.

### Final local verification, 2026-09-12

The complete Windows Python 3.14.5 suite passed **2,555 tests**, with two real
symlink-privilege skips, in **68.95 seconds**. Combined statement/branch coverage
is **98.4263%** (5,468/5,534 statements and 2,225/2,282 branches). Independent
Linux Python 3.12.3 full-source verification passed **2,557 tests with no skips**
in **196.96 seconds**, at **98.4007%** (5,467/5,534 and 2,224/2,282). Both retained
the original 95% gate and RuntimeWarning/ResourceWarning escalation. All 129
delivery-file hashes were unchanged across each full run.

Before the Linux full-source run, all **285 new configuration/CLI tests** passed
against the actual non-editable installed wheel with `python -I`, in **15.77
seconds**, including native POSIX no-replace publication and symlinks. Package
origins were verified at session start/end; all 29 runtime files match source
and wheel bytes. Its private environment was built using seven existing cached,
lock-hash-matched pytest/coverage dependencies, without network installation.
This installed test run and the explicit-source full run are distinct evidence.

The new CLI's final Windows full covers all 242 statements and 57/58 branches
(99.6667%, no exclusions). The configuration core has an independent 12,000-case
mutation/roundtrip review in addition to its handwritten direct-pipeline vectors.
The Windows Python 3.12.0 CLI repeat passed 141 tests with the same two privilege
skips; those skips are not counted as passes. Root's independent Windows CLI
rerun also passed 141 tests with two skips and all warnings treated as errors.

Ruff lint/format (108 files), strict Mypy (28 modules), Bandit and the unchanged
62-package offline lock pass. Sdist-to-wheel, strict Twine and wheel-content
checks pass. A fresh offline Windows wheel-only environment ran the executable
example under `-I`, independently observing accepted `A`, six validator calls,
and refusal to overwrite a completed report. The complete binary audit checks
29 runtime files, four installed metadata files and all 120 sdist source entries.
Final acceptance documentation is the only change after full testing; packages
are rebuilt and checked afterward. Hosted exact-commit checks are a separate gate.
