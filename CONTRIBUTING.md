# Contributing

Thank you for improving Payload Palette. Small, focused contributions are easiest to review.

## Set up a development environment

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Run the same checks as CI:

```bash
python -m ruff check .
python -m ruff format --check .
python -m pytest --cov=payload_palette --cov-branch
python benchmarks/benchmark_ingress.py --repeats 3 --operations 2
python -m build
```

## Change guidelines

- Open an issue before making a broad contract, schema, or security-policy change.
- Keep the core dependency-free unless a dependency provides a clear security or maintenance benefit.
- Add tests for success, malformed input, and boundary behavior when changing a parser or policy.
- Preserve part order and stable error codes unless the change is intentionally breaking.
- Never add real secrets, private URLs, copyrighted fixtures, or large binary media.
- Regenerate the demo with `python examples/build_demo.py --output-dir demo-output` and compare it before changing checked-in assets.
- Describe benchmark fixtures as synthetic unless their documented provenance proves otherwise. Do
  not commit customer traffic, credentials, or benchmark output without environment and protocol
  metadata.

## Pull requests

A pull request should explain the user-visible behavior, security impact, tests run, and compatibility implications. By submitting a contribution, you agree that it is licensed under the MIT License in this repository.
