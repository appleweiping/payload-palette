"""Run declarative output CLI checks offline in an owned temporary directory."""

from __future__ import annotations

import json
import tempfile
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from typing import cast

from payload_palette import JSONValue
from payload_palette.cli import run


def run_example() -> dict[str, JSONValue]:
    configuration = {
        "kind": "payload-output-config",
        "version": 1,
        "schema": {"type": "string"},
        "rules": [
            {
                "id": "trim",
                "path": [],
                "validator": {"kind": "trimmed_string"},
                "on_fail": "fix",
            },
            {
                "id": "choice",
                "path": [],
                "validator": {
                    "kind": "string_choices",
                    "choices": ["A", "B"],
                    "fix_case": True,
                },
                "on_fail": "fix",
            },
        ],
    }
    with tempfile.TemporaryDirectory(prefix="payload-configured-") as directory:
        config_path, report_path = Path(directory) / "config.json", Path(directory) / "report.json"
        config_path.write_text(json.dumps(configuration), encoding="utf-8")
        with StringIO() as stdout, StringIO() as stderr:
            if run(["check-output-config", str(config_path)], stdout=stdout, stderr=stderr) != 0:
                raise RuntimeError("example configuration failed")
            args = ["validate-output", str(config_path), "-", "-o", str(report_path)]
            # This flag deliberately permits repaired model output in the report.
            args.append("--include-output")
            with TextIOWrapper(BytesIO(b'" a "'), encoding="utf-8") as stdin:
                if run(args, stdin=stdin, stdout=stdout, stderr=stderr) != 0:
                    raise RuntimeError("example validation failed")
            original = report_path.read_bytes()
            # A second invocation cannot overwrite an existing report.
            with TextIOWrapper(BytesIO(b'"B"'), encoding="utf-8") as stdin:
                if run(args, stdin=stdin, stdout=stdout, stderr=stderr) != 2:
                    raise RuntimeError("example exclusive publication failed")
            if report_path.read_bytes() != original:
                raise RuntimeError("example report changed")
            return cast(dict[str, JSONValue], json.loads(original))


if __name__ == "__main__":
    print(json.dumps(run_example(), sort_keys=True, ensure_ascii=True))
