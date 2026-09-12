"""Malformed provenance, protocol, comparison and reducer boundary regressions."""

from __future__ import annotations

import io
import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from benchmarks import corpus, corpus_cli, corpus_replay, corpus_worker
from benchmarks.corpus import CorpusConfig, canonical, corpus_plan, iter_cases


@pytest.fixture
def case():
    return next(iter_cases(CorpusConfig(repeats=1)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("seed_sha256", "x" * 64),
        ("envelope", "unknown"),
        ("family", "unknown"),
        ("comparison", "unknown"),
        ("raw", bytearray(b"a")),
        pytest.param("raw", b"x" * 262145, id="oversized-raw"),
        ("parameters", (("seed",),)),
        ("parameters", (("bad key", 1),)),
        ("parameters", (("seed", 1), ("seed", 2))),
        ("parameters", (("seed", -1),)),
        ("parameters", (("seed", True),)),
        ("parameters", (("name", "é"),)),
        ("profile", ((1, 1),)),
        ("profile", (("bad", 1),)),
        ("profile", (("parts", 0),)),
        ("profile", (("text", 1), ("text", 2))),
        ("profile", tuple(("text", 1) for _ in range(33))),
    ],
)
def test_malformed_case_admission(case, field, value) -> None:
    with pytest.raises(ValueError):
        replace(case, **{field: value})


@pytest.fixture
def provenance(tmp_path, monkeypatch):
    document = json.loads((corpus.DATA / "json_testsuite.json").read_bytes())
    notice = (corpus.DATA / "LICENSE.JSONTestSuite").read_bytes()
    (tmp_path / "LICENSE.JSONTestSuite").write_bytes(notice)
    monkeypatch.setattr(corpus, "DATA", tmp_path)

    def write(value):
        (tmp_path / "json_testsuite.json").write_bytes(canonical(value))

    return document, write


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_field",
        "wrong_version",
        "floating_version",
        "unpinned",
        "license",
        "notice",
        "selection",
        "transformation",
        "seed_count",
        "seed_shape",
        "path_type",
        "path_escape",
        "duplicate",
        "encoding_type",
        "encoding_size",
        "encoding_invalid",
        "size",
        "sha",
        "blob",
        "groups",
    ],
)
def test_full_provenance_gate(provenance, mutation) -> None:
    document, write = provenance
    if mutation == "unknown_field":
        document["extra"] = 1
    elif mutation == "wrong_version":
        document["version"] = 2
    elif mutation == "floating_version":
        document["version"] = 1.0
    elif mutation == "unpinned":
        document["commit"] = "main"
    elif mutation == "license":
        document["spdx"] = "unknown"
    elif mutation == "notice":
        (corpus.DATA / "LICENSE.JSONTestSuite").write_bytes(b"missing required notice")
    elif mutation in ("selection", "transformation"):
        document[mutation] = ""
    elif mutation == "seed_count":
        document["seeds"].pop()
    elif mutation == "seed_shape":
        document["seeds"][0]["extra"] = 1
    elif mutation == "path_type":
        document["seeds"][0]["path"] = []
    elif mutation == "path_escape":
        document["seeds"][0]["path"] = "test_parsing/../secret.json"
    elif mutation == "duplicate":
        document["seeds"][0]["path"] = document["seeds"][1]["path"]
    elif mutation == "encoding_type":
        document["seeds"][0]["base64"] = None
    elif mutation == "encoding_size":
        document["seeds"][0]["base64"] = "a" * 349529
    elif mutation == "encoding_invalid":
        document["seeds"][0]["base64"] = "!"
    elif mutation == "size":
        document["seeds"][0]["size"] = 8
    elif mutation == "sha":
        document["seeds"][0]["sha256"] = "0" * 64
    elif mutation == "blob":
        document["seeds"][0]["blob"] = "0" * 40
    elif mutation == "groups":
        document["seeds"][0]["path"] = "test_parsing/z_reclassified.json"
    write(document)
    with pytest.raises(ValueError):
        corpus.external_seeds()


def test_duplicate_ids_and_wrong_config_are_refused_before_a_parser(case, monkeypatch) -> None:
    with pytest.raises(ValueError):
        list(iter_cases(None))
    monkeypatch.setattr(corpus, "iter_cases", lambda _: iter((case, case)))
    with pytest.raises(ValueError, match="duplicate"):
        corpus_plan(CorpusConfig(repeats=1))


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("case", None),
        ("raw", 9),
        pytest.param("raw", "a" * 349529, id="oversized-encoding"),
        ("raw", "!"),
        ("parameters", None),
        ("parameters", [["a"]]),
        ("chunks", []),
        ("chunks", [True]),
    ],
)
def test_worker_protocol_admission(case, field, value) -> None:
    document = json.loads(corpus_worker.request_bytes(case, (1,)))
    if field in ("version", "case", "chunks"):
        document[field] = value
    else:
        document["case"][field] = value
    with pytest.raises(ValueError):
        corpus_worker.parse_request(canonical(document))


def test_request_encoding_ceiling(case, monkeypatch) -> None:
    monkeypatch.setattr(corpus_worker, "MAX_REQUEST", 1)
    with pytest.raises(ValueError, match="ceiling"):
        corpus_worker.request_bytes(case, (1,))


@pytest.mark.parametrize("mode", ["valid", "invalid", "control", "oversize"])
def test_child_fixed_entry_point_reports_all_outcomes(case, monkeypatch, mode) -> None:
    raw = corpus_worker.request_bytes(case, (1,)) if mode != "invalid" else b"{}"
    out = io.BytesIO()
    monkeypatch.setattr(corpus_worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(corpus_worker.sys, "stdout", SimpleNamespace(buffer=out))
    if mode == "control":

        def interrupted(*_args):
            raise KeyboardInterrupt()

        monkeypatch.setattr(corpus_worker, "compare_case", interrupted)
    if mode == "oversize":
        monkeypatch.setattr(corpus_worker, "MAX_RESPONSE", 1)
    corpus_worker.main()
    result = json.loads(out.getvalue())
    if mode == "valid":
        assert result["outcome"] == "match"
    else:
        assert "worker_error" in result


@pytest.mark.parametrize("mode", ["exit", "stderr", "oversize", "json", "wrong_case", "timeout"])
def test_watchdog_never_treats_protocol_failures_as_matches(case, monkeypatch, mode) -> None:
    def fake_run(*_args, **_kwargs):
        if mode == "timeout":
            raise subprocess.TimeoutExpired("fixed-worker", 1)
        return SimpleNamespace(
            returncode=1 if mode == "exit" else 0,
            stderr=b"diagnostic" if mode == "stderr" else b"",
            stdout=b"x" * 65537 if mode == "oversize" else b"not-json" if mode == "json" else b"{}",
        )

    monkeypatch.setattr(corpus_cli.subprocess, "run", fake_run)
    row = corpus_cli.guarded_compare(case, (1,), 1)
    assert row["outcome"] == ("timeout" if mode == "timeout" else "crash")
    assert row["case_id"] == case.case_id


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 121])
def test_watchdog_deadline_admission(case, timeout) -> None:
    with pytest.raises(ValueError):
        corpus_cli.guarded_compare(case, (1,), timeout)


@pytest.mark.parametrize("chunks", [(), (1, 1), ([],), (True,), [1]])
def test_schedule_admission_before_any_implementation(case, chunks) -> None:
    with pytest.raises(ValueError):
        corpus_replay.compare_case(case, chunks)


def test_actual_comparison_discrepancy_axes(case) -> None:
    from payload_palette import normalize_json_bytes

    def different(*_args, **_kwargs):
        return normalize_json_bytes(b'[{"type":"text","text":"different"}]')

    row = corpus_replay.compare_case(case, (1,), streamed=different)
    assert row["reason"] == "manifest_or_fingerprint"
    assert row["streams"][0]["axes"] == ["manifest_bytes", "fingerprint"]
    assert (
        corpus_replay.compare_case(case, (1,), streamed=lambda *a, **k: None)["reason"]
        == "implementation_exception"
    )


def test_reducer_records_every_accepted_deletion_and_budget_stop(case) -> None:
    row = {
        "outcome": "mismatch",
        "reason": "synthetic_oracle",
        "buffered": {"status": "accepted"},
        "streams": [],
    }
    candidate = replace(case, raw=b"abcdef")
    result = corpus_replay.minimize_case(candidate, (1,), observer=lambda _: row)
    assert result["final_bytes"] == 0
    assert all(step["accepted"] for step in result["steps"])
    assert result["total_replays"] == result["attempts"] + 1
    short = corpus_replay.minimize_case(
        candidate, (1,), max_processed_bytes=12, observer=lambda _: row
    )
    assert short["byte_budget_exhausted"] and short["attempts"] == 0
    with pytest.raises(ValueError, match="baseline"):
        corpus_replay.minimize_case(candidate, (1,), max_processed_bytes=1)
    with pytest.raises(ValueError, match="actual mismatch"):
        corpus_replay.minimize_case(case, (1,))


def test_actual_timeout_settles_the_owned_interpreter_not_a_venv_redirector(
    case, monkeypatch
) -> None:
    command, environment = corpus_cli._worker_command()
    real_run = subprocess.run
    completed = []

    def sleeping_worker(_command, **kwargs):
        # A private test substitution, never an input accepted by the public runner.
        try:
            return real_run([command[0], "-c", "import time; time.sleep(5)"], **kwargs)
        finally:
            completed.append(True)

    monkeypatch.setattr(corpus_cli.subprocess, "run", sleeping_worker)
    result = corpus_cli.guarded_compare(case, (1,), 0.1)
    assert result["outcome"] == "timeout"
    assert completed == [True]
    if corpus_cli.sys.platform == "win32" and environment is not None:
        assert command[0] != corpus_cli.sys.executable
        assert environment["__PYVENV_LAUNCHER__"] == corpus_cli.sys.executable


@pytest.mark.parametrize("mutation", ["missing", "issue", "schedule", "extra", "status", "hash"])
def test_worker_response_shape_is_checked_before_report_aggregation(case, monkeypatch, mutation):
    result = corpus_replay.compare_case(case, (1,))
    if mutation == "missing":
        result.pop("streams")
    elif mutation == "issue":
        result["buffered"] = {"status": "rejected", "issues": ["not-a-code-path-pair"]}
    elif mutation == "schedule":
        result["streams"][0]["chunk"] = True
    elif mutation == "extra":
        result["extra"] = "unknown protocol field"
    elif mutation == "status":
        result["streams"][0]["observation"]["status"] = "invented"
    else:
        result["buffered"]["fingerprint"] = []
    monkeypatch.setattr(
        corpus_cli.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stderr=b"", stdout=canonical(result)),
    )
    assert corpus_cli.guarded_compare(case, (1,), 1)["outcome"] == "crash"


def test_minimizer_admits_schedule_before_its_observer(case):
    calls = []
    with pytest.raises(ValueError, match="chunk"):
        corpus_replay.minimize_case(case, (), observer=lambda candidate: calls.append(candidate))
    assert calls == []
