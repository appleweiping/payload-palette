from __future__ import annotations

import json
from pathlib import Path

from examples.build_demo import render_svg

from payload_palette import normalize


def test_checked_in_demo_matches_current_public_api_byte_for_byte() -> None:
    root = Path(__file__).resolve().parents[1]
    request = json.loads((root / "examples" / "request.json").read_text(encoding="utf-8"))
    manifest = normalize(request).to_dict()
    expected_manifest = (
        json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode()
    expected_svg = render_svg(manifest).encode()
    assert (root / "examples" / "manifest.json").read_bytes() == expected_manifest
    assert (root / "docs" / "demo.svg").read_bytes() == expected_svg
