"""Shared payload fixtures."""

from __future__ import annotations

import base64

import pytest

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"payload-palette"
WAV_BYTES = (
    b"RIFF\x24\x00\x00\x00WAVEfmt "
    b"\x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00"
    b"\x02\x00\x10\x00data\x00\x00\x00\x00"
)


@pytest.fixture
def png_base64() -> str:
    return base64.b64encode(PNG_BYTES).decode()


@pytest.fixture
def png_data_url(png_base64: str) -> str:
    return f"data:image/png;base64,{png_base64}"


@pytest.fixture
def wav_base64() -> str:
    return base64.b64encode(WAV_BYTES).decode()
