# Security policy

## Supported versions

The latest release on the `main` branch receives security fixes. Pre-release branches are not supported.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could enable resource exhaustion, policy bypass, credential disclosure, or access to private network resources. Use GitHub's private security advisory feature for this repository. Include:

- the affected version or commit;
- a minimal payload that demonstrates the issue;
- expected and observed behavior;
- your assessment of impact;
- any suggested mitigation.

You should receive an acknowledgement within seven days. A fix, disclosure timeline, and credit will be coordinated through the advisory.

## Security model

Payload Palette treats request JSON, Base64, data URLs, MIME declarations, and remote URLs as untrusted. It does not fetch remote content. Remote references are rejected unless their host is explicitly allowlisted. This reduces exposure but does not make accepted URLs safe for a downstream fetcher; see [the security section in the README](README.md#security-model).

The CLI applies a pre-parse input-byte ceiling and rejects ambiguous/non-standard JSON. Programmatic
callers already holding a decoded Python object remain responsible for bounding construction of that
object. URL validation rejects syntactically ambiguous local-address spellings, but downstream
fetchers must still resolve, pin, and re-check every address and redirect to defend against DNS
rebinding.

Programmatic HTTP integrations should pass raw bytes through `decode_json_bytes()` or
`normalize_json_bytes()` so strict JSON checks are not lost. Server-side limits and the split of
responsibilities are documented in [API ingress integration](docs/ingress-integration.md) and the
[security policy matrix](benchmarks/security_policy_matrix.json).

Data URL headers are bounded before parameter parsing, and MIME allowlists or declaration conflicts
are evaluated before Base64 decoding. Public manifest count fields remain JSON-serializable at
CPython's minimum configurable integer-conversion limit. CLI file output uses a same-directory
temporary file and atomic replacement so handled write failures preserve an existing manifest.

Remote URL strings have a fixed pre-parse length ceiling. Literal address classification is kept
stable across supported Python versions with an explicit table for loopback, private, shared,
link-local, benchmark, documentation, translation, transition, multicast, and reserved ranges.
The table includes the deprecated IPv4 6to4 relay block and current globally reachable IETF IPv6
anycast exceptions.
