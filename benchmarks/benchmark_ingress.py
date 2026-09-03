"""Measure strict ingress normalization on deterministic synthetic payloads.

This is a local characterization tool, not a universal performance claim.
Run it on the deployment hardware and configuration you care about.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from payload_palette import __version__, normalize_json_bytes

SCHEMA_VERSION = 1
MAX_REPEATS = 100
MAX_OPERATIONS = 100_000


def _workloads() -> dict[str, bytes]:
    return {
        "one_text_part": json.dumps(
            [{"type": "text", "text": "deterministic ingress benchmark"}],
            separators=(",", ":"),
        ).encode(),
        "mixed_inline_parts": json.dumps(
            [
                {"type": "text", "text": "classify these observations"},
                {
                    "type": "image",
                    "mime_type": "image/png",
                    "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
                },
                {
                    "type": "audio",
                    "mime_type": "audio/wav",
                    "data": "UklGRiQAAABXQVZFZm10IA==",
                },
            ],
            separators=(",", ":"),
        ).encode(),
        "one_hundred_text_parts": json.dumps(
            [{"type": "text", "text": f"synthetic segment {index:03d}"} for index in range(100)],
            separators=(",", ":"),
        ).encode(),
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _measure(raw: bytes, repeats: int, operations: int) -> dict[str, Any]:
    expected = normalize_json_bytes(raw).fingerprint
    for _ in range(2):
        normalize_json_bytes(raw)

    elapsed: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        for _ in range(operations):
            if normalize_json_bytes(raw).fingerprint != expected:
                raise RuntimeError("normalization changed during the benchmark")
        elapsed.append(time.perf_counter() - started)

    tracemalloc.start()
    try:
        normalize_json_bytes(raw)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    latencies_us = [seconds * 1_000_000 / operations for seconds in elapsed]
    throughputs = [operations / seconds for seconds in elapsed]
    return {
        "input_bytes": len(raw),
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_fingerprint": expected,
        "latency_us_per_operation": {
            "median": round(statistics.median(latencies_us), 3),
            "p95": round(_percentile(latencies_us, 0.95), 3),
            "min": round(min(latencies_us), 3),
            "max": round(max(latencies_us), 3),
        },
        "throughput_operations_per_second": {
            "median": round(statistics.median(throughputs), 3),
            "min": round(min(throughputs), 3),
            "max": round(max(throughputs), 3),
        },
        "tracemalloc_peak_bytes_one_operation": peak_bytes,
    }


def run_benchmark(*, repeats: int = 7, operations: int = 250) -> dict[str, Any]:
    """Return environment-qualified measurements for all synthetic workloads."""

    if type(repeats) is not int or not 3 <= repeats <= MAX_REPEATS:
        raise ValueError(f"repeats must be an integer in [3, {MAX_REPEATS}]")
    if type(operations) is not int or not 1 <= operations <= MAX_OPERATIONS:
        raise ValueError(f"operations must be an integer in [1, {MAX_OPERATIONS}]")
    if tracemalloc.is_tracing():
        raise ValueError(
            "benchmark requires tracemalloc to be stopped so caller tracing is not modified"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "software": {"name": "payload-palette", "version": __version__},
        "dataset": {
            "kind": "synthetic",
            "generator": "benchmarks/benchmark_ingress.py::_workloads",
            "random_seed": None,
        },
        "measured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "operating_system": platform.platform(),
            "machine": platform.machine() or "unknown",
            "processor": platform.processor() or "unknown",
            "logical_cpu_count": os.cpu_count(),
        },
        "protocol": {
            "repeats": repeats,
            "operations_per_repeat": operations,
            "warmup_operations": 2,
            "timer": "time.perf_counter",
            "memory_probe": (
                "isolated tracemalloc session for one operation; run is rejected if tracing "
                "is already active"
            ),
        },
        "workloads": {
            name: _measure(raw, repeats, operations) for name, raw in _workloads().items()
        },
        "interpretation": (
            "Measurements characterize this recorded environment only; rerun on target hardware."
        ),
    }


def _write_atomic(path: Path, content: str) -> None:
    """Atomically replace one result file after a complete synchronized write."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name or 'payload-palette-benchmark'}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--operations", type=int, default=250)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = run_benchmark(repeats=args.repeats, operations=args.operations)
    except ValueError as exc:
        parser.error(str(exc))
    encoded = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is None:
        sys.stdout.write(encoded)
    else:
        _write_atomic(args.output, encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
