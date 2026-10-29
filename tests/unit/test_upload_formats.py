"""PDF and image structure checks, the ClamAV client and the safe YAML loader (see test_upload_validation.py for ZIPs)."""

from __future__ import annotations

import base64
import contextlib
import re
import socketserver
import struct
import threading
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import settings
from app.schemas import DeploymentFileUpload
from app.services import malware_scan
from app.services.app_spec import parse_checked
from app.services.deployment_input import attach_files_to_user_input
from app.services.upload_validation import UploadRejected, inspect_upload
from app.utils import safe_yaml
from tests.unit import upload_samples as samples

DATA = Path(__file__).parent / "data"


def rejected(name: str, data: bytes) -> str:
    with pytest.raises(UploadRejected) as exc:
        inspect_upload(name, data)
    return exc.value.code


# ------------------------------------------------------------------ PDF


@pytest.mark.unit
def test_a_real_pdflatex_pdf_with_links_and_object_streams_passes():
    data = (DATA / "pdflatex-hyperref.pdf").read_bytes()
    # the shape that must not cause a false alarm: object streams, and a link action inside one of them
    assert b"/ObjStm" in data
    inflated = b""
    for body in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        with contextlib.suppress(zlib.error):
            inflated += zlib.decompress(body)
    assert b"/URI" in inflated
    assert inspect_upload("Aufgabe.pdf", data).content_type == "application/pdf"


@pytest.mark.unit
@pytest.mark.parametrize(
    "extra, code",
    [
        (b"<< /S /JavaScript /JS (app.alert(1)) >>", "pdf_active_content"),
        (b"<< /S /Launch /F (cmd.exe) >>", "pdf_active_content"),
        (b"<< /Type /EmbeddedFile /Length 0 >>", "pdf_active_content"),
        (b"<< /Type /RichMedia >>", "pdf_active_content"),
        (b"<< /XFA [] >>", "pdf_active_content"),
        (b"<< /S /SubmitForm /F (http://x) >>", "pdf_active_content"),
        (
            b"<< /S /J#61vaScript /J#53 (x) >>",
            "pdf_active_content",
        ),  # names can be written with #xx escapes
        (b"<< /Filter /Standard /V 4 >>\n/Encrypt 3 0 R", "pdf_encrypted"),
    ],
)
def test_pdf_with_active_content_or_encryption_is_rejected(extra, code):
    assert rejected("a.pdf", samples.pdf(extra)) == code


@pytest.mark.unit
def test_javascript_hidden_in_a_compressed_object_stream_is_found():
    assert (
        rejected("a.pdf", samples.pdf(objstm=b"3 0 << /S /JavaScript /JS (x) >>"))
        == "pdf_active_content"
    )
    assert (
        inspect_upload(
            "a.pdf",
            samples.pdf(
                objstm=b"3 0 << /Type /Annot /Subtype /Link /A << /URI (https://example.org) >> >>"
            ),
        ).kind
        == "pdf"
    )


@pytest.mark.unit
def test_unreadable_object_streams_are_rejected():
    broken = samples.pdf().replace(
        b"trailer",
        b"4 0 obj\n<< /Type /ObjStm /Length 5 >>\nstream\nnot zlib\nendstream\nendobj\ntrailer",
    )
    assert rejected("a.pdf", broken) == "pdf_unscannable"
    bomb = samples.pdf(objstm=b"0" * (9 * 1024 * 1024))
    assert rejected("a.pdf", bomb) == "pdf_unscannable"


@pytest.mark.unit
def test_binary_stream_data_is_not_mistaken_for_active_content():
    noise = b"\x00/JS\x20/JavaScript/Launch" * 50  # inside an image stream: not a dictionary
    pdf = samples.pdf().replace(
        b"trailer",
        b"5 0 obj\n<< /Type /XObject /Subtype /Image /Length 1 >>\nstream\n"
        + noise
        + b"\nendstream\nendobj\ntrailer",
    )
    assert inspect_upload("a.pdf", pdf).kind == "pdf"


# --------------------------------------------------------------- images


@pytest.mark.unit
def test_valid_images_pass():
    assert inspect_upload("a.png", samples.png()).content_type == "image/png"
    assert inspect_upload("a.jpg", samples.jpeg()).content_type == "image/jpeg"
    assert inspect_upload("a.gif", samples.gif()).content_type == "image/gif"
    assert inspect_upload("a.webp", samples.webp()).content_type == "image/webp"


@pytest.mark.unit
def test_png_checks_every_chunk_and_the_end():
    good = samples.png()
    assert rejected("a.png", good[:-5]) == "image_invalid"  # truncated
    damaged = bytearray(good)
    damaged[20] ^= 0x01  # inside IHDR: the CRC no longer matches
    assert rejected("a.png", bytes(damaged)) == "image_invalid"
    assert (
        rejected("a.png", samples.png(trailing=b"PK\x03\x04appended archive"))
        == "image_trailing_data"
    )
    assert rejected("a.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32) == "image_invalid"


@pytest.mark.unit
def test_an_image_that_announces_too_many_pixels_is_rejected():
    assert rejected("a.png", samples.png(20000, 20000)) == "image_too_large"
    assert rejected("a.jpg", samples.jpeg(30000, 30000)) == "image_too_large"
    assert rejected("a.gif", samples.gif(60000, 60000)) == "image_too_large"
    assert inspect_upload("a.png", samples.png(4000, 4000)).kind == "png"


@pytest.mark.unit
def test_jpeg_gif_and_webp_must_be_whole():
    assert rejected("a.jpg", samples.jpeg(end=False)) == "image_invalid"
    assert rejected("a.jpg", b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 6) == "image_invalid"
    assert rejected("a.gif", samples.gif(trailer=False)) == "image_trailing_data"
    assert rejected("a.webp", samples.webp() + b"extra") == "image_trailing_data"


# ----------------------------------------------- hostile and unusual containers


def _objstm(inflated: bytes, number: int) -> bytes:
    packed = zlib.compress(inflated)
    return (
        f"{number} 0 obj\n<< /Type /ObjStm /N 1 /First 4 /Filter /FlateDecode /Length {len(packed)} >>\nstream\n".encode()
        + packed
        + b"\nendstream\nendobj\n"
    )


@pytest.mark.unit
def test_a_pdf_stream_that_never_ends_is_rejected():
    cut_off = samples.pdf().replace(
        b"trailer", b"5 0 obj\n<< /Length 9 >>\nstream\nno end in sight\ntrailer"
    )
    assert rejected("a.pdf", cut_off) == "pdf_invalid"


@pytest.mark.unit
def test_many_object_streams_together_may_not_exceed_the_inflate_budget():
    # Each stream is far below the per-stream limit, only the sum is too much (a small file inflating to ~35 MiB).
    def with_streams(count: int) -> bytes:
        streams = b"".join(_objstm(b"0" * (7 * 1024 * 1024), 10 + i) for i in range(count))
        return samples.pdf().replace(b"trailer", streams + b"trailer")

    assert inspect_upload("a.pdf", with_streams(4)).kind == "pdf"  # 28 MiB: within the budget
    assert rejected("a.pdf", with_streams(5)) == "pdf_unscannable"  # 35 MiB: over it


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


@pytest.mark.unit
def test_a_png_chunk_that_announces_more_data_than_the_file_has_is_truncated():
    announces_too_much = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4s", 1000, b"IDAT") + b"x" * 20
    assert rejected("a.png", announces_too_much) == "image_invalid"


@pytest.mark.unit
def test_a_png_must_start_with_a_header_chunk_even_when_its_checksums_are_right():
    no_header = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IDAT", b"\x00" * 5) + _png_chunk(b"IEND", b"")
    assert rejected("a.png", no_header) == "image_invalid"


@pytest.mark.unit
@pytest.mark.parametrize(
    "name, data",
    [
        ("a.png", samples.png(0, 5)),
        ("a.jpg", samples.jpeg(10, 0)),
        ("a.gif", samples.gif(0, 0)),
    ],
)
def test_an_image_without_a_size_is_rejected(name, data):
    assert rejected(name, data) == "image_invalid"


@pytest.mark.unit
def test_a_jpeg_with_fill_bytes_and_restart_markers_is_a_valid_jpeg():
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + b"\x08"
        + struct.pack(">HH", 1, 1)
        + b"\x01\x01\x11\x00"
    )
    sos = b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00"
    # Fill bytes (0xFF) may precede any marker and restart markers carry no length: neither is damage.
    data = b"\xff\xd8" + b"\xff" + app0 + b"\xff\xd0" + sof + sos + b"\x12\x34" + b"\xff\xd9"

    assert inspect_upload("a.jpg", data).kind == "jpeg"


@pytest.mark.unit
@pytest.mark.parametrize(
    "data",
    [
        b"\xff\xd8\xff\xe0\x00\x06"
        + b"\x00" * 8,  # after the first segment: a byte that is no marker start
        b"\xff\xd8\xff\xc0"
        + struct.pack(">H", 4)
        + b"\x00\x00",  # a frame header too short to hold a size
        b"\xff\xd8"
        + b"\xff\xe0"
        + struct.pack(">H", 16)
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\xff\xd9",
    ],
    ids=["broken-segment", "short-frame-header", "no-frame-header"],
)
def test_a_malformed_jpeg_is_rejected(data):
    assert rejected("a.jpg", data) == "image_invalid"


@pytest.mark.unit
def test_gif_and_webp_files_shorter_than_their_header_are_rejected():
    assert rejected("a.gif", b"GIF89a\x01\x00") == "image_invalid"
    assert rejected("a.webp", b"RIFF" + struct.pack("<I", 4) + b"WEBP") == "image_invalid"


def _webp(kind: bytes, payload: bytes) -> bytes:
    """A RIFF/WebP container of one chunk of ``kind`` with ``payload`` (10 bytes make the shortest valid header)."""
    chunk = kind + struct.pack("<I", len(payload)) + payload
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def _vp8x(width: int, height: int) -> bytes:
    return _webp(
        b"VP8X",
        b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little"),
    )


def _vp8(width: int, height: int, start_code: bytes = b"\x9d\x01\x2a") -> bytes:
    return _webp(b"VP8 ", b"\x10\x00\x00" + start_code + struct.pack("<HH", width, height))


@pytest.mark.unit
def test_lossy_and_extended_webp_files_pass_like_lossless_ones():
    assert inspect_upload("a.webp", _vp8x(640, 480)).content_type == "image/webp"
    assert inspect_upload("a.webp", _vp8(640, 480)).content_type == "image/webp"


@pytest.mark.unit
def test_webp_variants_are_checked_for_size_and_a_damaged_header():
    assert rejected("a.webp", _vp8x(20000, 20000)) == "image_too_large"
    assert (
        rejected("a.webp", _vp8(16383, 16383)) == "image_too_large"
    )  # the largest a lossy WebP can announce
    assert rejected("a.webp", _vp8(640, 480, start_code=b"\x00\x00\x00")) == "image_invalid"
    lossless = bytearray(samples.webp())
    lossless[20] = 0x00  # the lossless signature byte
    assert rejected("a.webp", bytes(lossless)) == "image_invalid"
    assert (
        rejected("a.webp", _webp(b"ABCD", b"\x00" * 10)) == "image_invalid"
    )  # not a WebP variant at all


# ----------------------------------------------------------- malware scan


class _FakeClamd(socketserver.BaseRequestHandler):
    reply = b"stream: OK\0"
    received: list[bytes] = []
    # What the daemon says to zVERSION (the fixture sets a database built just now).
    version = b""

    def handle(self):
        command = self.request.recv(10)
        if command == b"zVERSION\0":
            self.request.sendall(type(self).version)
            return
        assert command == b"zINSTREAM\0"
        data = b""
        while True:
            header = b""
            while len(header) < 4:
                header += self.request.recv(4 - len(header))
            (size,) = struct.unpack(">I", header)
            if size == 0:
                break
            chunk = b""
            while len(chunk) < size:
                chunk += self.request.recv(size - len(chunk))
            data += chunk
        type(self).received.append(data)
        self.request.sendall(type(self).reply)


def version_reply(age_days: float) -> bytes:
    """What clamd answers to zVERSION with a database built ``age_days`` ago."""
    built = datetime.now(UTC) - timedelta(days=age_days)
    return f"ClamAV 1.4.3/28166/{built.strftime('%a %b %d %H:%M:%S %Y')}\0".encode()


@pytest.fixture
def clamd():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeClamd)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeClamd.reply, _FakeClamd.received = b"stream: OK\0", []
    _FakeClamd.version = version_reply(0)
    malware_scan._db_age_cache.clear()
    old = (settings.CLAMD_HOST, settings.CLAMD_PORT)
    settings.CLAMD_HOST, settings.CLAMD_PORT = "127.0.0.1", server.server_address[1]
    yield _FakeClamd
    settings.CLAMD_HOST, settings.CLAMD_PORT = old
    server.shutdown()


@pytest.mark.unit
def test_scan_is_a_no_op_while_no_scanner_is_configured():
    assert settings.CLAMD_HOST == ""
    malware_scan.scan(b"anything")  # no exception, no connection


@pytest.mark.unit
def test_scan_streams_the_whole_file_and_accepts_a_clean_answer(clamd):
    data = bytes(range(256)) * 1000  # several chunks
    malware_scan.scan(data)
    assert clamd.received == [data]


@pytest.mark.unit
def test_scan_reports_the_signature_of_an_infected_file(clamd):
    clamd.reply = b"stream: Win.Test.EICAR_HDB-1 FOUND\0"
    with pytest.raises(malware_scan.Infected) as exc:
        malware_scan.scan(b"x")
    assert exc.value.signature == "Win.Test.EICAR_HDB-1"


@pytest.mark.unit
def test_scan_fails_closed_on_errors_and_when_the_daemon_is_down(clamd):
    clamd.reply = b"INSTREAM size limit exceeded. ERROR\0"
    with pytest.raises(malware_scan.ScanFailed):
        malware_scan.scan(b"x")
    settings.CLAMD_PORT = 1  # nothing listens there
    with pytest.raises(malware_scan.ScanFailed):
        malware_scan.scan(b"x", timeout=1)


def _upload(name: str, content: bytes) -> DeploymentFileUpload:
    return DeploymentFileUpload(
        name=name,
        content_b64=base64.b64encode(content).decode(),
        size=len(content),
        content_type="text/plain",
    )


VAR = {
    "name": "notes",
    "source": "terraform",
    "osType": "file",
    "osScope": "all",
    "varScope": "all",
    "fileExtensions": ["txt"],
}


@pytest.mark.unit
def test_an_infected_upload_is_refused_with_the_signature(clamd):
    clamd.reply = b"stream: Eicar-Signature FOUND\0"
    with pytest.raises(HTTPException) as exc:
        attach_files_to_user_input(
            {"terraform": {}}, {"notes": {"all": _upload("a.txt", b"hello")}}, [VAR]
        )
    assert exc.value.status_code == 422
    assert (
        exc.value.detail["code"] == "malware_detected"
        and "Eicar-Signature" in exc.value.detail["message"]
    )


@pytest.mark.unit
def test_an_upload_is_refused_with_503_while_the_scanner_is_down(clamd):
    settings.CLAMD_PORT = 1
    with pytest.raises(HTTPException) as exc:
        attach_files_to_user_input(
            {"terraform": {}}, {"notes": {"all": _upload("a.txt", b"hello")}}, [VAR]
        )
    assert exc.value.status_code == 503 and exc.value.detail["reason"] == "upload_scan_unavailable"


@pytest.mark.unit
def test_a_clean_upload_passes_when_the_scanner_is_up(clamd):
    out = attach_files_to_user_input(
        {"terraform": {}}, {"notes": {"all": _upload("a.txt", b"hello")}}, [VAR]
    )
    assert out["terraform"]["notes"]["all"]["name"] == "a.txt"
    assert clamd.received == [b"hello"]


# ------------------------------------------------------------- safe YAML


@pytest.mark.unit
def test_safe_yaml_loads_normal_documents():
    assert safe_yaml.load("a: 1\nb: [x, y]\n") == {"a": 1, "b": ["x", "y"]}


@pytest.mark.unit
def test_safe_yaml_refuses_alias_bombs_big_and_deeply_nested_documents():
    bomb = "a: &a [x,x,x,x,x,x,x,x,x]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\nc: &c [*b,*b,*b,*b,*b,*b,*b,*b,*b]\nd: [*c,*c,*c,*c,*c,*c,*c,*c,*c]\n"
    with pytest.raises(safe_yaml.UnsafeYaml, match="alias"):
        safe_yaml.load(bomb)
    with pytest.raises(safe_yaml.UnsafeYaml, match="larger"):
        safe_yaml.load("a: " + "x" * (300 * 1024))
    with pytest.raises(safe_yaml.UnsafeYaml, match="nested"):
        safe_yaml.load("[" * 100 + "]" * 100)


@pytest.mark.unit
def test_appstore_yaml_uses_the_safe_loader():
    with pytest.raises(HTTPException) as exc:
        parse_checked(b"a: &x [1]\nb: *x\n")
    assert exc.value.status_code == 422
    assert "alias" in exc.value.detail["errors"][0]["msg"]
