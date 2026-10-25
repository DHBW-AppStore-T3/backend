"""Unit tests für ``app.utils.app_image`` (data-URL ↔ bytes Round-trip)."""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from app.utils.app_image import (
    MAX_IMAGE_BYTES,
    build_image_data_url,
    parse_image_data_url,
)

# Minimales 1×1 transparentes PNG (gültige Signatur + IHDR + IDAT + IEND).
_TINY_PNG_BYTES = bytes(
    [
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
        0x89, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41,
        0x54, 0x78, 0x9C, 0x62, 0x00, 0x01, 0x00, 0x00,
        0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00,
        0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,
        0x42, 0x60, 0x82,
    ]
)


@pytest.mark.unit
def test_parse_returns_none_tuple_for_none_input() -> None:
    """None → (None, None) als Clear-Sentinel."""
    assert parse_image_data_url(None) == (None, None)


@pytest.mark.unit
def test_parse_returns_none_tuple_for_empty_string() -> None:
    """Empty string → (None, None) ist ein gültiger Clear-Sentinel."""
    assert parse_image_data_url("") == (None, None)


@pytest.mark.unit
def test_parse_valid_png_data_url_roundtrip() -> None:
    """Ein gültiges PNG-data-URL wird korrekt dekodiert."""
    encoded = base64.b64encode(_TINY_PNG_BYTES).decode("ascii")
    data_url = f"data:image/png;base64,{encoded}"
    payload, mime = parse_image_data_url(data_url)
    assert payload == _TINY_PNG_BYTES
    assert mime == "image/png"


@pytest.mark.unit
def test_parse_invalid_format_raises_422() -> None:
    """Eingaben, die nicht auf das data-URL-Schema passen, ergeben 422."""
    with pytest.raises(HTTPException) as exc:
        parse_image_data_url("not-a-data-url")
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "invalid_image_format"


@pytest.mark.unit
def test_parse_invalid_base64_raises_422() -> None:
    """Payload, das das Regex passiert aber als base64 unzulässig ist, ergibt 422 invalid_base64."""
    # Das Regex erlaubt nur ``[A-Za-z0-9+/=\s]+`` — Sonderzeichen wie ``!``
    # würden schon dort scheitern (invalid_image_format). Wir brauchen
    # einen String, der durchs Regex kommt aber von ``base64.b64decode``
    # mit ``validate=True`` abgelehnt wird, etwa mit falscher Padding-Länge.
    with pytest.raises(HTTPException) as exc:
        parse_image_data_url("data:image/png;base64,abcde")  # 5 Zeichen → kein gültiges base64 Padding
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "invalid_base64"


@pytest.mark.unit
def test_parse_oversized_payload_raises_413() -> None:
    """Payload größer als MAX_IMAGE_BYTES ergibt 413 image_too_large."""
    oversized = b"\x00" * (MAX_IMAGE_BYTES + 1)
    encoded = base64.b64encode(oversized).decode("ascii")
    data_url = f"data:image/png;base64,{encoded}"
    with pytest.raises(HTTPException) as exc:
        parse_image_data_url(data_url)
    assert exc.value.status_code == 413
    assert exc.value.detail["reason"] == "image_too_large"
    assert exc.value.detail["max_bytes"] == MAX_IMAGE_BYTES
    assert exc.value.detail["actual_bytes"] == len(oversized)


@pytest.mark.unit
def test_parse_mime_is_lowercased() -> None:
    """Großschreibung im Mime-Subtype wird normalisiert."""
    jpeg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"jpegbytes"
    encoded = base64.b64encode(jpeg).decode("ascii")
    data_url = f"data:image/JPEG;base64,{encoded}"
    payload, mime = parse_image_data_url(data_url)
    assert payload == jpeg
    assert mime == "image/jpeg"


@pytest.mark.unit
def test_parse_non_image_mime_raises_422() -> None:
    """Nicht-image-Mimes matchen die Regex nicht und ergeben 422."""
    encoded = base64.b64encode(b"%PDF-1.4").decode("ascii")
    data_url = f"data:application/pdf;base64,{encoded}"
    with pytest.raises(HTTPException) as exc:
        parse_image_data_url(data_url)
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "invalid_image_format"


@pytest.mark.unit
def test_build_returns_none_when_bytes_missing() -> None:
    """Fehlende Bytes ergeben None unabhängig vom Mime."""
    assert build_image_data_url(None, "image/png") is None


@pytest.mark.unit
def test_build_returns_none_when_mime_missing() -> None:
    """Fehlender Mime ergibt None unabhängig von den Bytes."""
    assert build_image_data_url(b"x", None) is None


@pytest.mark.unit
def test_build_returns_none_for_empty_bytes() -> None:
    """Leere Bytes triggern den Truthiness-Short-Circuit."""
    assert build_image_data_url(b"", "image/png") is None


@pytest.mark.unit
def test_build_returns_none_for_empty_mime() -> None:
    """Leerer Mime triggert den Truthiness-Short-Circuit."""
    assert build_image_data_url(b"abc", "") is None


@pytest.mark.unit
def test_build_encodes_to_expected_data_url() -> None:
    """``b"abc"`` mit ``image/png`` ergibt den dokumentierten String."""
    assert build_image_data_url(b"abc", "image/png") == "data:image/png;base64,YWJj"


@pytest.mark.unit
def test_build_then_parse_roundtrip_preserves_bytes_and_mime() -> None:
    """build → parse liefert die identischen Bytes und denselben Mime zurück."""
    data_url = build_image_data_url(_TINY_PNG_BYTES, "image/png")
    assert data_url is not None
    payload, mime = parse_image_data_url(data_url)
    assert payload == _TINY_PNG_BYTES
    assert mime == "image/png"
