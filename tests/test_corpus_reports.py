"""Protocol binding, source audit and reproducible CLI/reducer report evidence."""

from __future__ import annotations

import io
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from benchmarks import corpus, corpus_cli, corpus_replay, corpus_worker
from benchmarks.corpus import CorpusConfig, canonical, iter_cases

from payload_palette.errors import PayloadValidationError, ValidationIssue


@pytest.fixture
def case():
    return next(iter_cases(CorpusConfig(repeats=1)))


def refuses(*_args, **_kwargs):
    raise PayloadValidationError([ValidationIssue("invalid_json", "synthetic", "$")])


@pytest.mark.parametrize("kind", ["accept_reject", "valid_rejected", "exception", "diagnostics"])
def test_report_roundtrips_every_actual_failure_kind(case, kind):
    def broken(*_args, **_kwargs):
        raise RuntimeError("not retained")

    if kind == "accept_reject":
        row = corpus_replay.compare_case(case, (1,), streamed=refuses)
    elif kind == "valid_rejected":
        row = corpus_replay.compare_case(case, (1,), buffered=refuses, streamed=refuses)
    elif kind == "exception":
        row = corpus_replay.compare_case(case, (1,), streamed=broken)
    else:
        candidate = replace(case, comparison="single")
        row = corpus_replay.compare_case(candidate, (1,), buffered=refuses, streamed=refuses)
        case = candidate
    assert corpus_worker.parse_response(canonical(row), case, (1,)) == row


@pytest.mark.parametrize(
    "mutation",
    [
        "case",
        "streams",
        "observation",
        "empty_issues",
        "exception_type",
        "reason",
        "axes",
        "summary",
    ],
)
def test_response_validator_rejects_inconsistent_nested_contracts(case, mutation):
    row = corpus_replay.compare_case(case, (1,))
    if mutation == "case":
        row["case_id"] = "0" * 64
    elif mutation == "streams":
        row["streams"] = []
    elif mutation == "observation":
        row["buffered"] = []
    elif mutation == "empty_issues":
        row["buffered"] = {"status": "rejected", "issues": []}
    elif mutation == "exception_type":
        row["buffered"] = {"status": "exception", "type": None}
    elif mutation == "reason":
        row["streams"][0]["difference"] = "undeclared waiver"
    elif mutation == "axes":
        row["streams"][0]["axes"] = ["unrelated"]
    else:
        row["outcome"] = "mismatch"
    with pytest.raises(ValueError):
        corpus_worker.parse_response(canonical(row), case, (1,))


def test_shrinker_does_not_change_to_a_different_discrepancy(case):
    first = {"outcome": "mismatch", "reason": "first", "streams": []}
    later = {"outcome": "mismatch", "reason": "different", "streams": []}
    result = corpus_replay.minimize_case(
        replace(case, raw=b"abc"),
        (1,),
        observer=lambda candidate: first if candidate.raw == b"abc" else later,
    )
    assert result["final_bytes"] == 3 and not any(x["accepted"] for x in result["steps"])
    assert result["descriptor"]["raw_sha256"] == result["original_sha256"]
    assert result["chunks"] == [1] and result["max_attempts"] == 64


@pytest.mark.parametrize("name", ["corpus.py", "json_testsuite.json"])
def test_source_and_artifact_byte_limits_precede_generation(monkeypatch, name):
    real_open = Path.open

    def open_fixture(path, *args, **kwargs):
        if path.name == name:
            size = 1024 * 1024 if name.endswith(".py") else corpus.MAX_ARTIFACT_BYTES
            return io.BytesIO(b"x" * (size + 1))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_fixture)
    with pytest.raises(ValueError, match="exceeds bound"):
        corpus.corpus_plan(CorpusConfig(repeats=1))


@pytest.mark.parametrize("mode", ["missing", "timeout", "oserror", "exit"])
def test_provenance_does_not_invent_unavailable_git_identity(monkeypatch, mode):
    if mode == "missing":
        monkeypatch.setattr(corpus_cli.shutil, "which", lambda _: None)
    else:

        def unavailable(*_args, **_kwargs):
            if mode == "timeout":
                raise subprocess.TimeoutExpired("git", 5)
            if mode == "oserror":
                raise OSError("unavailable")
            return SimpleNamespace(returncode=1, stdout="")

        monkeypatch.setattr(corpus_cli.subprocess, "run", unavailable)
    data = corpus_cli._provenance()
    assert data["repository_commit"] is data["working_tree_dirty"] is None
    assert len(data["normalizer_sources_sha256"]) == 64


def test_provenance_source_hash_read_is_bounded(monkeypatch):
    real_open = Path.open

    def oversized(path, *args, **kwargs):
        if path.name == "__init__.py":
            return io.BytesIO(b"x" * (1024 * 1024 + 1))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", oversized)
    with pytest.raises(ValueError, match="audit bound"):
        corpus_cli._provenance()


@pytest.mark.parametrize("mode", ["native", "same", "missing", "relative"])
def test_owned_interpreter_selection_is_closed(monkeypatch, mode):
    monkeypatch.setattr(corpus_cli.sys, "platform", "linux" if mode == "native" else "win32")
    monkeypatch.setattr(
        corpus_cli.sys,
        "_base_executable",
        corpus_cli.sys.executable
        if mode == "same"
        else None
        if mode == "missing"
        else "relative.exe",
    )
    if mode in ("native", "same"):
        command, environment = corpus_cli._worker_command()
        assert command == [corpus_cli.sys.executable, "-m", "benchmarks.corpus_worker"]
        assert environment is None
    else:
        with pytest.raises(ValueError, match="base interpreter"):
            corpus_cli._worker_command()


def test_worker_spawn_failure_is_an_observation_not_an_abandoned_run(case, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("do not publish OS arguments")

    monkeypatch.setattr(corpus_cli.subprocess, "run", unavailable)
    row = corpus_cli.guarded_compare(case, (1,), 1)
    assert row["outcome"] == "crash" and row["reason"] == "worker_start"
    assert "do not publish" not in canonical(row).decode()


@pytest.fixture
def short_run(case, monkeypatch):
    other = replace(case, raw=b"{", comparison="single")
    monkeypatch.setattr(corpus_cli, "iter_cases", lambda _: iter((case, other)))
    monkeypatch.setattr(
        corpus_cli, "guarded_compare", lambda c, chunks, _: corpus_replay.compare_case(c, chunks)
    )
    return case, other


@pytest.mark.parametrize("mode", ["rules", "normalizer", "audit"])
def test_stability_failure_preserves_every_observed_row(short_run, monkeypatch, mode):
    original = corpus_cli.corpus_plan
    calls = []

    def plan(config):
        result = original(config)
        calls.append(1)
        if len(calls) == 2:
            if mode == "rules":
                return replace(result, rules_sha256="changed")
            if mode == "audit":
                raise OSError("missing source")
        return result

    monkeypatch.setattr(corpus_cli, "corpus_plan", plan)
    if mode == "normalizer":
        sources = iter(({"normalizer_sources_sha256": "a"}, {"normalizer_sources_sha256": "b"}))
        monkeypatch.setattr(corpus_cli, "_provenance", lambda: next(sources))
    report = corpus_cli.run_corpus(CorpusConfig(repeats=1, chunks=(1,)))
    assert len(report["semantic"]["cases"]) == 2
    assert report["semantic"]["stability_errors"] == [
        {
            "rules": "corpus_changed",
            "normalizer": "normalizer_changed",
            "audit": "final_source_audit_failed",
        }[mode]
    ]
    assert report["semantic"]["counts"]["issue_code"]["invalid_json"] == 2


def test_replay_selection_and_cli_errors_do_not_overwrite_reports(short_run, tmp_path):
    first, _ = short_run
    output = tmp_path / "replay.json"
    assert (
        corpus_cli.main(
            ["replay", "--repeats", "1", "--case-id", first.case_id, "--output", str(output)]
        )
        == 0
    )
    result = json.loads(output.read_bytes())
    assert len(result["semantic"]["cases"]) == 1
    assert "--timeout" in result["semantic"]["cases"][0]["replay"]
    for mode in ("replay", "minimize"):
        assert corpus_cli.main([mode, "--output", str(tmp_path / "missing.json")]) == 2
        assert (
            corpus_cli.main(
                [
                    mode,
                    "--repeats",
                    "1",
                    "--case-id",
                    "unknown",
                    "--output",
                    str(tmp_path / "unknown.json"),
                ]
            )
            == 2
        )
    assert not (tmp_path / "missing.json").exists()
    assert not (tmp_path / "unknown.json").exists()


@pytest.mark.parametrize("stability", ["same", "changed", "unreadable"])
def test_cli_minimization_retains_plan_environment_and_failure_history(
    short_run, tmp_path, monkeypatch, stability
):
    case, _ = short_run

    def discrepancy(c, _chunks, _timeout):
        return {"outcome": "mismatch", "reason": "injected", "streams": [], "case_id": c.case_id}

    monkeypatch.setattr(corpus_cli, "guarded_compare", discrepancy)
    calls = []

    def provenance():
        calls.append(1)
        if len(calls) > 1 and stability == "unreadable":
            raise OSError("cannot read final source")
        return {"normalizer_sources_sha256": "a" if len(calls) == 1 or stability == "same" else "b"}

    monkeypatch.setattr(corpus_cli, "_provenance", provenance)
    output = tmp_path / "reduction.json"
    code = corpus_cli.main(
        [
            "minimize",
            "--repeats",
            "1",
            "--case-id",
            case.case_id,
            "--attempts",
            "2",
            "--output",
            str(output),
        ]
    )
    assert code == (0 if stability == "same" else 1)
    report = json.loads(output.read_bytes())
    assert report["plan"]["rules_sha256"] and report["environment"]
    assert report["original_sha256"] == case.raw_sha256
    assert report["attempts"] == 2 and report["total_replays"] == 3


def test_all_admitted_recipes_execute_offline_without_resolving_urls(monkeypatch):
    import socket

    def no_network(*_args, **_kwargs):
        raise AssertionError("offline corpus must not use network")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    cases = tuple(iter_cases(CorpusConfig(repeats=1)))
    for case in cases:
        result = corpus_replay.compare_case(case, (7,))
        assert result["outcome"] in ("match", "expected_precedence"), result
        assert corpus_worker.parse_response(canonical(result), case, (7,)) == result


@pytest.mark.parametrize("changed", ["status", "manifest_sha256", "fingerprint", "axes"])
def test_response_cannot_claim_match_against_its_actual_observations(case, changed):
    row = corpus_replay.compare_case(case, (7,))
    observation = row["streams"][0]["observation"]
    if changed == "status":
        row["streams"][0]["observation"] = {"status": "rejected", "issues": [["invalid_json", "$"]]}
    elif changed == "axes":
        row["streams"][0]["axes"] = ["fingerprint"]
    else:
        observation[changed] = ("sha256:" if changed == "fingerprint" else "") + "0" * 64
    with pytest.raises(ValueError):
        corpus_worker.parse_response(canonical(row), case, (7,))


def test_huge_timeout_is_rejected_without_float_conversion(case):
    with pytest.raises(ValueError, match="timeout"):
        corpus_cli.guarded_compare(case, (7,), 10**1000)


def test_generation_failure_after_an_observation_retains_partial_evidence(short_run, monkeypatch):
    case, _ = short_run

    def failed_generation(_config):
        yield case
        raise ValueError("injected generation failure, not payload data")

    monkeypatch.setattr(corpus_cli, "iter_cases", failed_generation)
    report = corpus_cli.run_corpus(CorpusConfig(repeats=1, chunks=(1,)))
    assert len(report["semantic"]["cases"]) == 1
    assert report["semantic"]["stability_errors"] == ["corpus_iteration_failed"]


def test_descriptor_identity_rejects_boolean_integer_aliases(case):
    row = corpus_replay.compare_case(case, (1,))
    row["descriptor"]["generator_version"] = True
    with pytest.raises(ValueError, match="identity"):
        corpus_worker.parse_response(canonical(row), case, (1,))


def test_reduction_preserves_failed_trials_and_cli_does_not_report_them_successful(
    short_run, tmp_path, monkeypatch
):
    case, _ = short_run
    calls = []

    def observation(candidate, _chunks, _timeout):
        calls.append(candidate)
        if len(calls) == 1:
            return corpus_replay.compare_case(candidate, (1,), streamed=lambda *a, **k: None)
        return {"outcome": "timeout", "reason": "worker_deadline", "case_id": candidate.case_id}

    monkeypatch.setattr(corpus_cli, "guarded_compare", observation)
    output = tmp_path / "failed-trial.json"
    code = corpus_cli.main(
        [
            "minimize",
            "--repeats",
            "1",
            "--case-id",
            case.case_id,
            "--attempts",
            "2",
            "--output",
            str(output),
        ]
    )
    report = json.loads(output.read_bytes())
    assert code == 1
    assert report["baseline"]["streams"][0]["observation"]["type"] == "builtins.TypeError"
    assert report["counts"]["timeout"] == 2
    assert all(step["observation"]["outcome"] == "timeout" for step in report["steps"])


@pytest.mark.parametrize("outcome", ["crash", "timeout"])
def test_unavailable_reduction_baseline_is_evidence_not_a_false_reduction(case, outcome):
    result = corpus_replay.minimize_case(
        case, (1,), observer=lambda _: {"outcome": outcome, "reason": "injected"}
    )
    assert result["baseline_failed"] and result["attempts"] == 0
    assert result["total_replays"] == 1 and result["counts"][outcome] == 1
    assert result["original_sha256"] == result["final_sha256"]
