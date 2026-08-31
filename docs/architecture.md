# Architecture

Payload Palette is an ingress contract, not a media pipeline. Its job ends after an untrusted JSON value has been converted into a validated, deterministic, binary-free manifest. This narrow boundary keeps parsing decisions and security policy testable without model runtimes, codecs, network clients, or cloud SDKs.

## Design invariants

The implementation maintains seven invariants:

1. **Order is semantic.** Every normalized part keeps the ordinal assigned while traversing the request. Parts are never grouped by type or sorted.
2. **No implicit I/O.** Payload strings can become text, decoded inline bytes, or validated remote references. They never become filesystem paths, DNS queries, or HTTP requests.
3. **Parse and decode within budgets.** CLI bytes, JSON depth and integer width, flattened parts,
   data-URL header length, Base64 length, decoded bytes, and aggregate inline bytes all have explicit
   bounds.
4. **Policy and output are immutable.** Policies validate exact runtime types and hard ceilings;
   caller mappings, manifest parts, and part attributes are copied into read-only values.
5. **Output is safe to inspect, not automatically safe to persist.** Binary payloads and remote query strings are omitted, while prompt text remains visible by design.
6. **Failures point back to input.** Stable error codes are paired with the precise retained value
   path and independent part errors are aggregated when possible.
7. **Published files are transactional.** CLI file output is written, flushed, and synchronized in
   the destination directory before atomic replacement; failed writes preserve the previous file.

## Module map

| Module | Responsibility |
|---|---|
| `errors.py` | Immutable issues, stable validation envelope, and output-write failures. |
| `models.py` | Internal `PartSpec` plus immutable normalized part and manifest values. |
| `parser.py` | Envelope discovery, type aliases, ordered traversal, field extraction. |
| `media.py` | MIME normalization, bounded Base64/data-URL decode, signature recognition. |
| `policy.py` | Resource limits, MIME allowlists, host patterns, remote URL validation. |
| `normalizer.py` | Per-source pipelines, error aggregation, fingerprints, canonical manifest. |
| `cli.py` | JSON file/stdin handling, policy flags, exit codes, JSON/human errors. |

Dependencies point inward toward models and errors. Neither parsing nor policy imports the CLI. The core has no third-party runtime dependency.

## Pipeline

### 1. Envelope discovery

`extract_entries` recognizes four roots:

- an array of content parts;
- an object with a `content` array;
- an object with `messages`, whose message content may be a string or array;
- one object with a `type` field.

Messages are traversed in array order and each content array is traversed in place. The flattened sequence receives monotonically increasing ordinals while retaining its original path.

Envelope recognition is deliberately closed. Silently searching arbitrary nested objects could accept unintended fields, increase work unpredictably, and make error locations ambiguous.
The CLI rejects duplicate object keys and non-standard/non-finite JSON numbers before envelope
discovery. It reads files and stdin in bounded chunks and rejects excessive nesting. Entry collection
stops at the configured part limit instead of first materializing an unbounded flattened list.

### 2. Part parsing

External type aliases map into four `PartKind` values. `PartSpec` retains only information needed for normalization:

- ordinal, part path, and exact source value path;
- semantic kind;
- source string and source hint;
- declared MIME type;
- a small allowlist of presentation attributes.

No Base64 is decoded in the parser. This keeps external-shape errors separate from resource and content errors.

### 3. Source-specific normalization

Text is checked against the `text/plain` MIME policy, then UTF-8 encoded for byte accounting and
fingerprinting. Its semantic string is retained.

Inline media accepts a base64 data URL or bare Base64 with an explicit/inferred MIME type. Before
extracting the encoded portion, the data-URL parser caps its header at 1,024 characters and accepts
only one `base64` marker. MIME declarations, kind allowlists, and conflicts are checked before byte
decoding. Before materializing a compact copy, the decoder bounds both non-whitespace encoded characters and
whitespace amplification from the decoded-byte policy. It then removes ASCII whitespace, repairs
only wholly omitted padding, rejects nonstandard characters, partial/excess padding, and non-zero
padding bits, and checks the final decoded length. Recognized signatures are compared after MIME
allowlist checks.

Remote media passes through `RemoteURLPolicy`. Before allowlist matching, the policy rejects URL
lengths above 16,384 characters, controls, whitespace, backslashes, malformed percent escapes and
host labels, credentials, IPv6 zones, local addresses, and ambiguous legacy IPv4 spellings. Host IP
classification uses an explicit version-stable special-address table instead of changing
`ipaddress.is_global` data. The canonical form lowercases scheme/IDNA host, removes one host trailing
dot plus default ports/dot segments/fragments, decodes unreserved percent escapes, and uppercases
retained escapes. Multiple trailing dots are rejected. Dot-segment removal is linear in path length.
Query order, reserved delimiters, and Unicode path normalization are left unchanged. The canonical
query is fingerprinted; the display locator can redact it.

The table conservatively blocks the deprecated IPv4 6to4 relay block, including the non-global
6a44 relay address, while retaining globally reachable IETF anycast exceptions in `2001::/23`,
including the SRP anycast address. Tests pin these decisions on every supported Python version.

### 4. Request-wide checks and canonicalization

Successful text and inline parts contribute to a running `inline_bytes` total. The pipeline stops as soon as that total crosses its request budget, avoiding needless decoding of later parts. Parts are serialized with sorted dictionary keys, compact JSON separators, and standard-JSON finite-number enforcement, then hashed in original order. Changing normalized bytes, text, URLs, metadata, paths, or order changes the request fingerprint. Public model count fields additionally stop at 640 decimal digits, which remains serializable when CPython's configurable integer-string limit is set to its minimum legal value.

## Error aggregation

Shape errors within separate messages or content entries are aggregated during parsing. Media normalization similarly accumulates independent per-part errors. The pipeline stops between phases if an earlier phase is invalid because later assumptions would no longer hold. Request-wide errors are evaluated only after all parts normalize successfully.

`PayloadValidationError` never exists without at least one `ValidationIssue`. Its dictionary form is also the CLI's JSON error contract.

Successful JSON sent to stdout converts write failures such as a closed pipe into the stable
`output_write` contract. Named output files use a same-directory temporary file and `os.replace`;
temporary files are removed on every handled failure.

## Remote URL policy and SSRF boundary

URL validation is intentionally non-networked. This makes the function deterministic and prevents validation itself from becoming an SSRF primitive. It also means a hostname that resolves to a private address cannot be detected here.

Canonical IPv4/IPv6 literals are interpreted with `ipaddress`. Numeric, shortened, octal, or
hexadecimal IPv4 spellings that another URL stack could reinterpret are rejected rather than treated
as DNS names. Hostname allowlisting still cannot prevent DNS rebinding, so the downstream duties
below remain mandatory.

A downstream fetcher must:

1. resolve with a trusted resolver;
2. reject every non-global resolved address;
3. connect to the checked address while preserving TLS hostname validation;
4. apply the same checks to every redirect;
5. cap headers, compressed bytes, decoded bytes, duration, and response time;
6. verify response MIME and decode in a sandbox appropriate to the format.

An allowlisted hostname expresses application intent; it is not proof that every future response is trusted.

## Fingerprints

Part fingerprints use SHA-256 over:

- UTF-8 bytes for text;
- decoded bytes for inline media;
- the documented syntactic canonical URL for remote references.

The manifest fingerprint hashes canonical JSON for all normalized parts. Prefixing values with `sha256:` makes algorithm migrations explicit. Fingerprints can deduplicate work or correlate logs, but an unkeyed digest is not an authorization token and does not authenticate the producer.

## Extension points

### Add an envelope adapter

Add an explicit branch in `extract_entries`, retaining exact paths and array order. Add tests for empty, malformed, and mixed message content. Do not recursively search unknown dictionaries.

### Add a media representation

Translate it into `PartSpec` in `parser.py`; reuse the existing inline or remote pipeline. A new encoding requires its own bounded decoder in `media.py` and adversarial tests.

### Add a MIME type

Update the kind-specific allowlist. If a reliable lightweight signature exists, add it to `detect_mime_type`. Signature detection must never parse unbounded structures or decompress media.

### Add a network fetcher

Keep it outside this package. A fetcher has DNS, redirects, TLS, streaming, decompression, and sandbox responsibilities that conflict with Payload Palette's deterministic no-I/O contract.

## Testing strategy

Unit tests operate entirely on bounded in-memory payloads. Parameterized malformed-input cases cover
strict JSON, policy runtime types, Unicode, URL parser differentials, canonical Base64, decoder and
signature branches; normalizer tests cover ordering, immutability, canonical equivalence, and policy
composition; CLI tests inject bounded streams and temporary files. The demo builder is executed in CI
and compares its JSON and SVG outputs byte-for-byte with committed assets.

CI runs the suite with branch coverage on Python 3.11 through 3.14 plus a Windows 3.14 job, performs
Ruff linting/format checks, verifies generated artifacts without platform-specific shell tools, and
builds source and wheel distributions.
