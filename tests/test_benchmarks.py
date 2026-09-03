from __future__ import annotations

import hashlib
import json
import os
import tracemalloc
from pathlib import Path

import pytest
from benchmarks.benchmark_ingress import (
    MAX_OPERATIONS,
    MAX_REPEATS,
    SCHEMA_VERSION,
    _percentile,
    _workloads,
    _write_atomic,
    run_benchmark,
)

from payload_palette import __version__, normalize_json_bytes


def test_benchmark_is_environment_qualified_and_machine_readable() -> None:
    result = run_benchmark(repeats=3, operations=2)

    assert result["schema_version"] == SCHEMA_VERSION
    assert result["software"] == {"name": "payload-palette", "version": __version__}
    assert result["dataset"]["kind"] == "synthetic"
    assert result["protocol"]["repeats"] == 3
    assert result["environment"]["python"]
    assert set(result["workloads"]) == {
        "mixed_inline_parts",
        "one_hundred_text_parts",
        "one_text_part",
    }
    for measurement in result["workloads"].values():
        assert measurement["input_bytes"] > 0
        assert len(measurement["input_sha256"]) == 64
        assert measurement["manifest_fingerprint"].startswith("sha256:")
        assert measurement["latency_us_per_operation"]["median"] > 0
        assert measurement["throughput_operations_per_second"]["median"] > 0
        assert measurement["tracemalloc_peak_bytes_one_operation"] > 0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    ("repeats", "operations"),
    [
        (2, 1),
        (MAX_REPEATS + 1, 1),
        (3, 0),
        (3, MAX_OPERATIONS + 1),
        (True, 1),
    ],
)
def test_benchmark_rejects_invalid_protocol(repeats: object, operations: object) -> None:
    with pytest.raises(ValueError):
        run_benchmark(repeats=repeats, operations=operations)  # type: ignore[arg-type]


def test_percentile_interpolates_without_external_dependencies() -> None:
    assert _percentile([4.0, 1.0, 2.0, 3.0], 0.5) == 2.5


def test_benchmark_rejects_existing_tracemalloc_without_stopping_it() -> None:
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="caller tracing"):
            run_benchmark(repeats=3, operations=1)
        assert tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()


def test_atomic_benchmark_output_preserves_existing_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "result.json"
    destination.write_text("original\n", encoding="utf-8")

    def fail_replace(source: object, target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        _write_atomic(destination, "replacement\n")

    assert destination.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.iterdir()) == [destination]


def test_security_policy_matrix_has_explicit_library_and_deployment_owners() -> None:
    path = Path(__file__).parents[1] / "benchmarks" / "security_policy_matrix.json"
    matrix = json.loads(path.read_text(encoding="utf-8"))

    assert matrix["schema_version"] == 1
    profiles = matrix["configuration_compatibility"]
    assert len({profile["profile"] for profile in profiles}) == len(profiles)
    assert any(not profile["require_https"] for profile in profiles)
    assert any(not profile["redact_query"] for profile in profiles)
    assert any(not profile["verify_known_signatures"] for profile in profiles)
    rows = matrix["rows"]
    assert len(rows) >= 8
    assert len({row["threat"] for row in rows}) == len(rows)
    assert all(row["library_control"] and row["deployment_control"] for row in rows)


def test_checked_in_reference_result_records_protocol_and_environment() -> None:
    path = Path(__file__).parents[1] / "benchmarks" / "results" / "reference-windows-python314.json"
    result = json.loads(path.read_text(encoding="utf-8"))

    assert result["schema_version"] == SCHEMA_VERSION
    assert result["software"] == {"name": "payload-palette", "version": __version__}
    assert result["dataset"]["kind"] == "synthetic"
    assert result["environment"]["python"]
    assert result["protocol"]["repeats"] >= 3
    assert result["protocol"]["operations_per_repeat"] >= 1
    assert set(result["workloads"]) == {
        "mixed_inline_parts",
        "one_hundred_text_parts",
        "one_text_part",
    }
    for name, raw in _workloads().items():
        measurement = result["workloads"][name]
        assert measurement["input_bytes"] == len(raw)
        assert measurement["input_sha256"] == hashlib.sha256(raw).hexdigest()
        assert measurement["manifest_fingerprint"] == normalize_json_bytes(raw).fingerprint
        assert set(measurement) == {
            "input_bytes",
            "input_sha256",
            "manifest_fingerprint",
            "latency_us_per_operation",
            "throughput_operations_per_second",
            "tracemalloc_peak_bytes_one_operation",
        }
