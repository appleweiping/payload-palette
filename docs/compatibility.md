# Compatibility and versioning

Payload Palette follows semantic versioning after the `0.x` development series. During `0.x`, a
minor release may revise the accepted input contract; patch releases preserve it. Every release
documents contract changes in `CHANGELOG.md`.

The compatibility surfaces are:

- public names exported from `payload_palette`;
- manifest `schema_version`, field meanings, ordering, and fingerprints;
- validation error `code` values (message prose is not stable);
- CLI command names, exit codes, and documented options;
- strict JSON and security-policy defaults.

Removing an accepted representation, weakening a security default, changing fingerprint input, or
changing a field under the same manifest schema requires an explicit migration note. Additive
manifest fields require consumers to ignore unknown fields. A new incompatible manifest uses a new
schema version.

Python versions listed in `pyproject.toml` are exercised in CI. Support for a Python version is
removed only in a documented minor release and not earlier than that Python version's upstream
security end-of-life unless maintenance becomes infeasible. No operating-system-specific behavior
is part of the payload contract.
