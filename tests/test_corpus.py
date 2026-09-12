"""The offline corpus is a falsification tool, not a matching-results filter."""

from __future__ import annotations

from dataclasses import replace

import pytest
from benchmarks.corpus import CorpusConfig, corpus_plan, iter_cases
from benchmarks.corpus_replay import compare_case, minimize_case

from payload_palette import normalize_json_bytes


def test_manifest_and_recipes_preflight_before_replay() -> None:
    config = CorpusConfig(seed=19, repeats=1)
    plan = corpus_plan(config)
    cases = list(iter_cases(config))
    assert plan.case_count == len(cases) > 100
    assert plan.input_bytes == sum(len(case.raw) for case in cases)
    assert plan.executions == len(cases) * (1 + len(config.chunks))
    assert len({case.case_id for case in cases}) == len(cases)
    assert {case.envelope for case in cases} == {"default", "anthropic", "gemini", "ollama"}
    assert list(iter_cases(config)) == cases
    with pytest.raises(ValueError, match="case"):
        corpus_plan(replace(config, max_cases=1))


def test_favorable_results_are_not_a_condition_of_replay() -> None:
    case = next(case for case in iter_cases(CorpusConfig(repeats=1)) if case.comparison == "valid")
    baseline = compare_case(case, (1, 7))
    assert baseline["outcome"] == "match"

    def broken_stream(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("deliberate independent defect")

    result = compare_case(case, (1, 7), streamed=broken_stream)
    assert result["outcome"] == "mismatch"
    assert result["reason"] == "implementation_exception"
    assert len(result["streams"]) == 2


def test_minimizer_keeps_original_and_a_bounded_replayable_chain() -> None:
    case = next(case for case in iter_cases(CorpusConfig(repeats=1)) if case.comparison == "valid")

    def defective(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("deliberate independent defect")

    result = minimize_case(case, (7,), max_attempts=5, streamed=defective)
    assert result["original_sha256"] == case.raw_sha256
    assert result["attempts"] <= 5
    assert result["final_bytes"] <= len(case.raw)
    assert result["steps"]


def test_independent_invalid_token_cannot_pass_by_common_mode_acceptance() -> None:
    case = next(case for case in iter_cases(CorpusConfig(repeats=1)) if "/n_" in case.seed_id)
    valid = normalize_json_bytes(b'[{"type":"text","text":"injected valid result"}]')

    def accepts_anything(*_args: object, **_kwargs: object) -> object:
        return valid

    result = compare_case(case, (1,), buffered=accepts_anything, streamed=accepts_anything)
    assert result["outcome"] == "mismatch"
    assert result["reason"] == "malformed_control_accepted"


def test_descriptor_is_identical_after_json_wire_roundtrip() -> None:
    import json

    from benchmarks.corpus import canonical

    case = next(iter_cases(CorpusConfig(repeats=1)))
    assert json.loads(canonical(case.descriptor())) == case.descriptor()


def test_exception_observation_never_formats_the_implementation_error() -> None:
    class BrokenMessage(Exception):
        def __str__(self) -> str:
            raise ValueError("formatting must not execute")

    def broken(*_args: object, **_kwargs: object) -> object:
        raise BrokenMessage()

    case = next(iter_cases(CorpusConfig(repeats=1)))
    result = compare_case(case, (1,), streamed=broken)
    assert result["reason"] == "implementation_exception"
    assert result["streams"][0]["observation"]["type"].endswith("BrokenMessage")


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile", [["urlsafe", 0]]),
        ("profile", (("urlsafe", 2),)),
        ("parameters", (("seed", []),)),
        ("source_id", []),
        ("precedence", (("invalid_json", "duplicate_json_key"),)),
    ],
)
def test_case_metadata_is_closed_and_immutable(field: str, value: object) -> None:
    case = next(iter_cases(CorpusConfig(repeats=1)))
    with pytest.raises(ValueError):
        replace(case, **{field: value})


def test_shrinking_signature_preserves_which_accepted_result_axis_differs() -> None:
    from benchmarks.corpus_replay import _signature

    def row(axis: str) -> dict[str, object]:
        return {
            "outcome": "mismatch",
            "reason": "manifest_or_fingerprint",
            "buffered": {"status": "accepted"},
            "streams": [
                {
                    "chunk": 1,
                    "difference": "manifest_or_fingerprint",
                    "axes": [axis],
                    "observation": {"status": "accepted"},
                }
            ],
        }

    assert _signature(row("manifest_bytes")) != _signature(row("fingerprint"))


def test_actual_fixed_child_process_roundtrip() -> None:
    from benchmarks.corpus_cli import guarded_compare

    case = next(iter_cases(CorpusConfig(repeats=1)))
    result = guarded_compare(case, (1, 7), 20.0)
    assert result["outcome"] == "match", result


@pytest.mark.parametrize(
    "changes",
    [
        {"seed": True},
        {"seed": -1},
        {"seed": 2**32},
        {"repeats": 0},
        {"repeats": 17},
        {"chunks": [1]},
        {"chunks": ()},
        {"chunks": (1, 1)},
        {"chunks": (True,)},
        {"chunks": ([],)},
        {"chunks": (0,)},
        {"chunks": (262145,)},
        {"smoke_modulus": 0},
        {"max_cases": 10001},
        {"max_input_bytes": 0},
        {"max_executions": 100001},
        {"max_processed_bytes": 268435457},
    ],
)
def test_configuration_hard_ceilings(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CorpusConfig(**changes)


@pytest.mark.parametrize(
    "field", ["max_cases", "max_input_bytes", "max_executions", "max_processed_bytes"]
)
def test_all_declared_work_is_admitted_before_normalization(field: str) -> None:
    with pytest.raises(ValueError):
        corpus_plan(replace(CorpusConfig(repeats=1), **{field: 1}))


def test_all_foreign_labels_keep_independent_acceptance_semantics() -> None:
    from benchmarks.corpus import external_seeds
    from benchmarks.corpus_cases import foreign_cases

    for case in foreign_cases(external_seeds()):
        label = dict(case.parameters)["upstream_class"]
        assert case.comparison == {"y": "valid", "n": "invalid", "i": "equivalent"}[label]


def test_single_fault_diagnostic_difference_is_never_whitelisted() -> None:
    from payload_palette.errors import PayloadValidationError, ValidationIssue

    case = next(case for case in iter_cases(CorpusConfig(repeats=1)) if case.comparison == "single")

    def refuses(code: str):
        def implementation(*_args: object, **_kwargs: object) -> object:
            raise PayloadValidationError([ValidationIssue(code, "irrelevant message", "$")])

        return implementation

    result = compare_case(
        case, (1,), buffered=refuses("input_encoding"), streamed=refuses("invalid_json")
    )
    assert result["outcome"] == "mismatch"
    assert result["reason"] == "diagnostics"
    multi = replace(case, comparison="multiple", precedence=(("input_encoding", "invalid_json"),))
    assert (
        compare_case(
            multi, (1,), buffered=refuses("input_encoding"), streamed=refuses("invalid_json")
        )["outcome"]
        == "expected_precedence"
    )


def test_precedence_does_not_accept_overloaded_string_equality() -> None:
    class AlwaysEqual(str):
        def __eq__(self, _other: object) -> bool:
            return True

    case = next(iter_cases(CorpusConfig(repeats=1)))
    with pytest.raises(ValueError):
        replace(case, comparison="multiple", precedence=((AlwaysEqual("wrong"), "wrong"),))


def test_worker_request_cannot_import_or_evaluate_case_metadata() -> None:
    import json

    from benchmarks.corpus import canonical
    from benchmarks.corpus_worker import parse_request, request_bytes

    case = next(iter_cases(CorpusConfig(repeats=1)))
    raw = request_bytes(case, (1, 7))
    restored, chunks = parse_request(raw)
    assert restored == case and chunks == (1, 7)
    document = json.loads(raw)
    document["case"]["eval"] = "raise SystemExit"
    with pytest.raises(ValueError):
        parse_request(canonical(document))


def test_cli_keeps_all_zero_and_nonzero_outcome_categories(tmp_path, monkeypatch) -> None:
    import json

    from benchmarks import corpus_cli

    def timeout(case, _chunks, _timeout):
        return {
            "case_id": case.case_id,
            "descriptor": case.descriptor(),
            "outcome": "timeout",
            "reason": "injected_watchdog_expiry",
        }

    monkeypatch.setattr(corpus_cli, "guarded_compare", timeout)
    output = tmp_path / "report.json"
    assert corpus_cli.main(["run", "--repeats", "1", "--smoke", "--output", str(output)]) == 1
    report = json.loads(output.read_bytes())
    counts = report["semantic"]["counts"]["outcome"]
    assert counts["timeout"] == report["semantic"]["plan"]["case_count"] > 0
    assert counts["match"] == counts["crash"] == counts["mismatch"] == 0
    before = output.read_bytes()
    assert corpus_cli.main(["plan", "--repeats", "1", "--output", str(output)]) == 2
    assert output.read_bytes() == before
