from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from examples.generate_with_async_checks import run_example


def test_offline_example_complete_report():
    report = asyncio.run(run_example())
    assert report == {
        "valid": True,
        "termination": "accepted",
        "reported_tokens": 22,
        "response_bytes": 40,
        "validator_invocations": 10,
        "usage_complete": True,
        "output": {"label": "ready"},
        "attempts": [
            {
                "number": 1,
                "status": "invalid_output",
                "usage": {"input_tokens": 8, "output_tokens": 3},
                "response_bytes": 20,
                "validator_invocations": 5,
                "feedback": [{"code": "not_in_catalog", "path": '$["label"]'}],
            },
            {
                "number": 2,
                "status": "accepted",
                "usage": {"input_tokens": 8, "output_tokens": 3},
                "response_bytes": 20,
                "validator_invocations": 5,
                "feedback": [],
            },
        ],
    }


@pytest.mark.parametrize("optimized", [False, True])
def test_offline_example_actual_entrypoint_outside_checkout(tmp_path, optimized):
    example = Path(__file__).resolve().parents[1] / "examples" / "generate_with_async_checks.py"
    process = subprocess.run(
        [sys.executable, *(("-O",) if optimized else ()), str(example)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(process.stdout) == asyncio.run(run_example())
    assert process.stderr == ""
