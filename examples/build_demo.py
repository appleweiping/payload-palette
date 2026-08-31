"""Rebuild the checked-in demo manifest and SVG from examples/request.json."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from payload_palette import normalize


def render_svg(manifest: dict[str, object]) -> str:
    """Render a small, dependency-free visualization of an actual manifest."""

    parts = manifest["parts"]
    assert isinstance(parts, list)
    colors = {
        "text": "#6ee7b7",
        "image": "#60a5fa",
        "audio": "#c084fc",
        "video": "#fb7185",
    }
    cards: list[str] = []
    for index, part in enumerate(parts):
        assert isinstance(part, dict)
        kind = str(part["kind"])
        source = str(part["source"])
        mime_type = str(part.get("mime_type", "unknown"))
        byte_length = str(part.get("byte_length", "remote"))
        x = 36 + index * 220
        cards.extend(
            [
                f'  <rect x="{x}" y="142" width="188" height="126" rx="14" '
                f'fill="#172033" stroke="{colors[kind]}" stroke-width="2"/>',
                f'  <text x="{x + 18}" y="174" class="kind">'
                f"{index + 1}. {html.escape(kind)}</text>",
                f'  <text x="{x + 18}" y="202" class="meta">source  {html.escape(source)}</text>',
                f'  <text x="{x + 18}" y="225" class="meta">'
                f"mime    {html.escape(mime_type)}</text>",
                f'  <text x="{x + 18}" y="248" class="meta">'
                f"bytes   {html.escape(byte_length)}</text>",
            ]
        )
        if index < len(parts) - 1:
            cards.append(
                f'  <path d="M {x + 189} 205 L {x + 216} 205" stroke="#64748b" '
                'stroke-width="2" marker-end="url(#arrow)"/>'
            )
    fingerprint = html.escape(str(manifest["fingerprint"]))
    lines = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="760" height="360" viewBox="0 0 760 360">',
        "  <defs>",
        '    <marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">',
        '      <path d="M0,0 L8,4 L0,8 Z" fill="#64748b"/>',
        "    </marker>",
        "    <style>",
        "      .title { font: 700 24px system-ui, sans-serif; fill: #f8fafc; }",
        "      .subtitle { font: 14px system-ui, sans-serif; fill: #94a3b8; }",
        "      .kind { font: 700 17px system-ui, sans-serif; fill: #f8fafc; }",
        "      .meta { font: 13px ui-monospace, monospace; fill: #cbd5e1; }",
        "      .hash { font: 12px ui-monospace, monospace; fill: #94a3b8; }",
        "    </style>",
        "  </defs>",
        '  <rect width="760" height="360" rx="20" fill="#0b1120"/>',
        '  <text x="36" y="50" class="title">Payload Palette &#183; normalized request</text>',
        '  <text x="36" y="78" class="subtitle">Original order retained &#183; '
        "binary data omitted &#183; fingerprints computed</text>",
        '  <rect x="36" y="96" width="688" height="1" fill="#253148"/>',
        *cards,
        f'  <text x="36" y="312" class="hash">{fingerprint}</text>',
        "</svg>",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if generated output differs from the checked-in manifest and SVG",
    )
    args = parser.parse_args()
    request_path = Path(__file__).with_name("request.json")
    document = json.loads(request_path.read_text(encoding="utf-8"))
    manifest = normalize(document).to_dict()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (args.output_dir / "demo.svg").write_text(render_svg(manifest), encoding="utf-8", newline="\n")
    if args.check:
        references = {
            "manifest.json": Path(__file__).with_name("manifest.json"),
            "demo.svg": Path(__file__).parents[1] / "docs" / "demo.svg",
        }
        mismatches = [
            name
            for name, reference in references.items()
            if (args.output_dir / name).read_bytes() != reference.read_bytes()
        ]
        if mismatches:
            raise SystemExit(f"generated demo differs: {', '.join(mismatches)}")


if __name__ == "__main__":
    main()
