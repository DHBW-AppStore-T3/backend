"""PDF and image structure checks, the ClamAV client and the safe YAML loader (see test_upload_validation.py for ZIPs)."""

from __future__ import annotations

import base64
import contextlib
import re
import socketserver
import struct
import threading
import zlib
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


# ----------------------------------------------------------- malware scan


class _FakeClamd(socketserver.BaseRequestHandler):
    reply = b"stream: OK\0"
    received: list[bytes] = []

    def handle(self):
        assert self.request.recv(10) == b"zINSTREAM\0"
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


@pytest.fixture
def clamd():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _FakeClamd)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _FakeClamd.reply, _FakeClamd.received = b"stream: OK\0", []
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
            {"terraform": {}, "packer": {}}, {"notes": {"all": _upload("a.txt", b"hello")}}, [VAR]
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
            {"terraform": {}, "packer": {}}, {"notes": {"all": _upload("a.txt", b"hello")}}, [VAR]
        )
    assert exc.value.status_code == 503 and exc.value.detail["reason"] == "upload_scan_unavailable"


@pytest.mark.unit
def test_a_clean_upload_passes_when_the_scanner_is_up(clamd):
    out = attach_files_to_user_input(
        {"terraform": {}, "packer": {}}, {"notes": {"all": _upload("a.txt", b"hello")}}, [VAR]
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
