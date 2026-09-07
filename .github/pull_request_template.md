## Purpose

Describe the user-visible contract change and why it is needed.

## Security and compatibility

- Threat or trust-boundary impact:
- Manifest, fingerprint, error-code, or CLI compatibility impact:
- Migration required:

## Verification

- [ ] Added success, malformed-input, and boundary tests where applicable
- [ ] `python -m ruff check .`
- [ ] `python -m ruff format --check .`
- [ ] `python -m pytest --cov=payload_palette --cov-branch`
- [ ] `python examples/build_demo.py --output-dir demo-output --check`
- [ ] `python -m build --no-isolation`
- [ ] Documentation and changelog updated
