from __future__ import annotations

import json
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path

import pytest

from payload_palette import __version__
from payload_palette.cli import (
    MAX_CLI_INPUT_BYTES,
    MAX_JSON_DEPTH,
    _write_json,
    _write_json_file_atomic,
    run,
)
from payload_palette.errors import OutputError


def test_cli_reports_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        run(["--version"])
    assert error.value.code == 0
    assert capsys.readouterr().out == f"payload-palette {__version__}\n"


def test_validate_stdin() -> None:
    stdout = StringIO()
    code = run(
        ["validate", "-"],
        stdin=StringIO('[{"type":"text","text":"hello"}]'),
        stdout=stdout,
        stderr=StringIO(),
    )
    result = json.loads(stdout.getvalue())
    assert code == 0
    assert result["valid"] is True
    assert result["part_count"] == 1


def test_normalize_compact_stdout() -> None:
    stdout = StringIO()
    code = run(
        ["normalize", "-", "--compact"],
        stdin=StringIO('[{"type":"text","text":"hello"}]'),
        stdout=stdout,
        stderr=StringIO(),
    )
    assert code == 0
    assert "\n " not in stdout.getvalue()
    assert json.loads(stdout.getvalue())["parts"][0]["text"] == "hello"


def test_normalize_to_file(tmp_path: Path) -> None:
    input_path = tmp_path / "request.json"
    output_path = tmp_path / "manifest.json"
    input_path.write_text('[{"type":"text","text":"hello"}]', encoding="utf-8")
    assert run(["normalize", str(input_path), "-o", str(output_path)]) == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["part_count"] == 1


def test_invalid_json_has_structured_error() -> None:
    stderr = StringIO()
    code = run(
        ["validate", "-", "--json-errors"],
        stdin=StringIO("{"),
        stdout=StringIO(),
        stderr=stderr,
    )
    result = json.loads(stderr.getvalue())
    assert code == 2
    assert result["errors"][0]["code"] == "invalid_json"


def test_duplicate_json_keys_are_rejected() -> None:
    stderr = StringIO()
    code = run(
        ["validate", "-", "--json-errors"],
        stdin=StringIO('[{"type":"image","type":"text","text":"ambiguous"}]'),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "duplicate_json_key"


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonstandard_json_numbers_are_rejected(constant: str) -> None:
    stderr = StringIO()
    payload = f'{{"content":[],"ignored":{constant}}}'
    code = run(
        ["validate", "-", "--json-errors"],
        stdin=StringIO(payload),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == ("nonstandard_json_number")


@pytest.mark.parametrize("number", ["1e9999", "9" * 257])
def test_out_of_range_json_numbers_are_rejected(number: str) -> None:
    stderr = StringIO()
    payload = f'{{"content":[],"ignored":{number}}}'
    code = run(
        ["validate", "-", "--json-errors"],
        stdin=StringIO(payload),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "json_number_range"


def test_json_nesting_depth_is_bounded() -> None:
    stderr = StringIO()
    payload = "[" * (MAX_JSON_DEPTH + 1) + "0" + "]" * (MAX_JSON_DEPTH + 1)
    code = run(
        ["validate", "-", "--json-errors"],
        stdin=StringIO(payload),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "json_too_deep"


def test_stdin_is_read_in_bounded_chunks() -> None:
    class ReadSpy(StringIO):
        def __init__(self, value: str) -> None:
            super().__init__(value)
            self.calls: list[int] = []

        def read(self, size: int = -1) -> str:
            self.calls.append(size)
            return super().read(size)

    stream = ReadSpy('[{"type":"text","text":"ok"}]')
    assert run(["validate", "-"], stdin=stream, stdout=StringIO(), stderr=StringIO()) == 0
    assert stream.calls
    assert all(size > 0 for size in stream.calls)


def test_stdin_input_byte_limit_is_enforced() -> None:
    stderr = StringIO()
    code = run(
        ["validate", "-", "--max-input-bytes", "10", "--json-errors"],
        stdin=StringIO('[{"type":"text","text":"too large"}]'),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "input_too_large"


def test_file_input_byte_limit_is_enforced(tmp_path: Path) -> None:
    path = tmp_path / "large.json"
    path.write_text('[{"type":"text","text":"too large"}]', encoding="utf-8")
    stderr = StringIO()
    code = run(
        ["validate", str(path), "--max-input-bytes", "10", "--json-errors"],
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "input_too_large"


def test_invalid_utf8_file_is_an_input_encoding_error(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(b'[{"type":"text","text":"\xff"}]')
    stderr = StringIO()
    code = run(
        ["validate", str(path), "--json-errors"],
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "input_encoding"


def test_isolated_surrogate_is_not_mislabeled_as_policy_error() -> None:
    stderr = StringIO()
    code = run(
        ["validate", "-", "--json-errors"],
        stdin=StringIO('[{"type":"text","text":"\\ud800"}]'),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "invalid_unicode"


@pytest.mark.parametrize(
    "payload",
    [
        '[{"type":"text","text":"ok","ignored":"\\ud800"}]',
        '[{"type":"text","text":"ok","\\udfff":1}]',
        '{"messages":[{"role":"\\ud800","content":"ok"}]}',
    ],
)
def test_isolated_surrogates_in_ignored_json_strings_are_rejected(payload: str) -> None:
    stderr = StringIO()
    code = run(
        ["validate", "-", "--json-errors"],
        stdin=StringIO(payload),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "invalid_unicode"


def test_input_limit_has_a_hard_ceiling() -> None:
    stderr = StringIO()
    code = run(
        [
            "validate",
            "-",
            "--max-input-bytes",
            str(MAX_CLI_INPUT_BYTES + 1),
            "--json-errors",
        ],
        stdin=StringIO("[]"),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "invalid_policy"


def test_json_writer_refuses_nonstandard_nan() -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        _write_json({"value": float("nan")}, StringIO())


def test_json_stdout_falls_back_to_ascii_escapes_for_a_narrow_encoding() -> None:
    raw = BytesIO()
    stdout = TextIOWrapper(raw, encoding="ascii", errors="strict")
    payload = json.dumps([{"type": "text", "text": "你好 😀"}])
    assert (
        run(
            ["normalize", "-", "--compact"],
            stdin=StringIO(payload),
            stdout=stdout,
            stderr=StringIO(),
        )
        == 0
    )
    stdout.flush()
    result = json.loads(raw.getvalue().decode("ascii"))
    assert result["parts"][0]["text"] == "你好 😀"


def test_human_stderr_uses_backslash_escapes_for_a_narrow_encoding() -> None:
    raw = BytesIO()
    stderr = TextIOWrapper(raw, encoding="ascii", errors="strict")
    payload = json.dumps([{"type": "未知 😀"}])
    assert run(["validate", "-"], stdin=StringIO(payload), stdout=StringIO(), stderr=stderr) == 2
    stderr.flush()
    message = raw.getvalue().decode("ascii")
    assert message.startswith("payload-palette: $")
    assert "\\U0001f600" in message


def test_json_stderr_falls_back_to_ascii_escapes_for_a_narrow_encoding() -> None:
    raw = BytesIO()
    stderr = TextIOWrapper(raw, encoding="ascii", errors="strict")
    payload = json.dumps([{"type": "未知 😀"}])
    assert (
        run(
            ["validate", "-", "--json-errors"],
            stdin=StringIO(payload),
            stdout=StringIO(),
            stderr=stderr,
        )
        == 2
    )
    stderr.flush()
    result = json.loads(raw.getvalue().decode("ascii"))
    assert result["errors"][0]["code"] == "unknown_type"


def test_remote_cli_requires_and_accepts_allowlist() -> None:
    payload = '[{"type":"image_url","image_url":"https://cdn.example.org/a.png"}]'
    denied_error = StringIO()
    assert (
        run(
            ["validate", "-", "--json-errors"],
            stdin=StringIO(payload),
            stdout=StringIO(),
            stderr=denied_error,
        )
        == 2
    )
    assert json.loads(denied_error.getvalue())["errors"][0]["code"] == "remote_url_disabled"

    allowed_output = StringIO()
    assert (
        run(
            ["validate", "-", "--allow-host", "cdn.example.org"],
            stdin=StringIO(payload),
            stdout=allowed_output,
            stderr=StringIO(),
        )
        == 0
    )


def test_missing_file_reports_read_error(tmp_path: Path) -> None:
    stderr = StringIO()
    code = run(
        ["validate", str(tmp_path / "missing.json"), "--json-errors"],
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "input_read"


def test_plain_text_error_output() -> None:
    stderr = StringIO()
    code = run(
        ["validate", "-"],
        stdin=StringIO("{"),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert stderr.getvalue().startswith("payload-palette: $")


def test_invalid_cli_policy_is_reported_as_json() -> None:
    stderr = StringIO()
    code = run(
        ["validate", "-", "--max-parts", "0", "--json-errors"],
        stdin=StringIO('[{"type":"text","text":"hello"}]'),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "invalid_policy"


def test_output_write_error_is_structured(tmp_path: Path) -> None:
    stderr = StringIO()
    code = run(
        ["normalize", "-", "-o", str(tmp_path), "--json-errors"],
        stdin=StringIO('[{"type":"text","text":"hello"}]'),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "output_write"


def test_stdout_broken_pipe_is_a_stable_output_error() -> None:
    class BrokenOutput(StringIO):
        def write(self, value: str) -> int:
            raise BrokenPipeError("consumer closed the pipe")

    stderr = StringIO()
    code = run(
        ["validate", "-", "--json-errors"],
        stdin=StringIO('[{"type":"text","text":"hello"}]'),
        stdout=BrokenOutput(),
        stderr=stderr,
    )
    result = json.loads(stderr.getvalue())
    assert code == 2
    assert result["errors"][0]["code"] == "output_write"
    assert result["errors"][0]["path"] == "-"


def test_atomic_output_failure_preserves_existing_file_and_cleans_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "manifest.json"
    output.write_text("original\n", encoding="utf-8")

    def fail_after_partial_write(value: object, stream: object, compact: bool = False) -> None:
        del value, compact
        stream.write("partial")  # type: ignore[attr-defined]
        raise OSError("simulated disk failure")

    monkeypatch.setattr("payload_palette.cli._write_json", fail_after_partial_write)
    with pytest.raises(OutputError, match="cannot write output"):
        _write_json_file_atomic({"valid": True}, output)

    assert output.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []


def test_non_ascii_windows_compatible_paths_and_lf_output(tmp_path: Path) -> None:
    input_path = tmp_path / "请求.json"
    output_path = tmp_path / "清单.json"
    input_path.write_text('[{"type":"text","text":"你好"}]', encoding="utf-8")
    assert run(["normalize", str(input_path), "-o", str(output_path)]) == 0
    payload = output_path.read_bytes()
    assert b"\r\n" not in payload
    assert json.loads(payload.decode("utf-8"))["parts"][0]["text"] == "你好"
