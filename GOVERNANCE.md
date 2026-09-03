# Governance

Payload Palette is maintainer-led. The current release maintainer is
[@appleweiping](https://github.com/appleweiping). Design discussion, compatibility changes, and
roadmap work happen in public issues and pull requests; vulnerabilities follow the private process
in [SECURITY.md](SECURITY.md).

Changes to accepted payloads, policy defaults, fingerprints, stable error codes, or resource limits
require tests, a compatibility note, and an updated changelog. Benchmark claims must include the
workload, environment, complete machine-readable result, and the limitations described in
[research-limitations.md](docs/research-limitations.md).

A release requires green CI, reviewed generated artifacts, wheel and source-distribution checks, an
isolated installation smoke test, checksums, and build provenance. Maintainer roles and decision
rules may evolve through an explicit pull request as the contributor base grows.

