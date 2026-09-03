# Evaluation scope and limitations

Payload Palette is an ingress contract and defensive parser, not a learned system. Its tests and
benchmarks establish deterministic behavior, resource ceilings, and performance characteristics;
they do not establish model quality, malware-detection accuracy, or end-to-end service security.

The benchmark workloads in `benchmarks/benchmark_ingress.py` are generated synthetic requests. They
are intentionally small and auditable and must not be described as production traffic. Timing and
`tracemalloc` measurements are environment-specific. They exclude HTTP parsing, network transfer,
process scheduling under load, DNS, fetching, decompression, media parsing, and model inference.
Run the benchmark on target hardware with representative, legally usable traffic before sizing a
deployment.

The benchmark refuses to run while caller-owned `tracemalloc` tracing is active because stopping or
resetting that process-global tracer would corrupt the caller's measurement. Its own isolated probe
is stopped in a `finally` block. CLI result files use same-directory atomic replacement; timing
limits are bounded but do not make benchmark execution an untrusted multi-tenant workload.

Signature checks cover recognizable leading bytes and are not a file-validity classifier. URL
classification does not resolve names and therefore cannot measure or prevent DNS rebinding by
itself. The repository contains no claim that the policy catches every malformed media container,
SSRF technique, or denial-of-service strategy. See `SECURITY.md` and the machine-readable policy
matrix for the exact ownership boundary.
