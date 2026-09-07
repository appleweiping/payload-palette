from __future__ import annotations

from payload_palette import NormalizationPolicy, RemoteURLPolicy, audit_manifest, normalize


def test_audit_receipt_does_not_retain_text_or_url_secrets() -> None:
    manifest = normalize(
        [
            {"type": "text", "text": "private prompt"},
            {"type": "image_url", "image_url": {"url": "https://cdn.example/a.png?token=secret"}},
        ],
        NormalizationPolicy(
            remote=RemoteURLPolicy(allowed_hosts=("cdn.example",), redact_query=False)
        ),
    )
    receipt = audit_manifest(manifest)
    document = receipt.to_dict()
    rendered = str(document)
    assert "private prompt" not in rendered
    assert "token=secret" not in rendered
    assert document["parts"][0]["text_present"] is True
    assert document["parts"][1]["locator"] == "https://cdn.example/a.png"
