"""Unit tests for ``app.services.upload_validation`` (content checks for deployment uploads).

Every hostile archive here is built by the test; nothing is downloaded.
"""

from __future__ import annotations

import base64
import io
import stat
import struct
import zipfile
import zlib

import pytest
from fastapi import HTTPException

from app.schemas import DeploymentFileUpload
from app.services import upload_validation as uv
from app.services.deployment_input import attach_files_to_user_input
from app.services.upload_validation import UploadRejected, inspect_upload
from app.utils.app_image import parse_image_data_url
from tests.unit import upload_samples as samples

PNG = samples.png()
JPEG = samples.jpeg()
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
ELF = b"\x7fELF" + b"\x00" * 60


def make_zip(
    entries: dict[str, bytes | tuple[bytes, int]], compress: int = zipfile.ZIP_DEFLATED
) -> bytes:
    """A ZIP from ``name -> content`` (or ``name -> (content, unix_mode)``)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, value in entries.items():
            content, mode = value if isinstance(value, tuple) else (value, 0o100644)
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            info.compress_type = compress
            z.writestr(info, content)
    return buf.getvalue()


def rejected(name: str, data: bytes, exts: list[str] | None = None) -> str:
    with pytest.raises(UploadRejected) as exc:
        inspect_upload(name, data, exts)
    return exc.value.code


# ------------------------------------------------------------------ ZIP


@pytest.mark.unit
def test_good_zip_passes_and_reports_its_size():
    data = make_zip(
        {"aufgaben/": b"", "aufgaben/A1.java": b"class A {}", "aufgaben/A2.java": b"class B {}"}
    )
    result = inspect_upload("aufgaben.zip", data, ["zip"])
    assert (result.kind, result.content_type, result.zip_entries) == ("zip", "application/zip", 3)
    assert result.zip_unpacked_bytes == len(b"class A {}") + len(b"class B {}")


@pytest.mark.unit
@pytest.mark.parametrize(
    "name, code",
    [
        ("../../etc/passwd", "zip_path_traversal"),
        ("a/../../b", "zip_path_traversal"),
        ("/etc/passwd", "zip_absolute_path"),
        ("C:/Windows/x", "zip_absolute_path"),
        ("a\nb", "zip_bad_name"),
        ("./x", "zip_path_traversal"),
        ("a//b", "zip_path_traversal"),
        ("x" * 300, "zip_bad_name"),
        ("/".join(["d"] * 40) + "/f", "zip_bad_name"),
    ],
)
def test_zip_with_dangerous_paths_is_rejected_as_a_whole(name, code):
    assert rejected("x.zip", make_zip({"ok.txt": b"fine", name: b"x"})) == code


@pytest.mark.unit
@pytest.mark.parametrize("name", ["a\\b", "a\x00b", "..\\x", "C:\\x"])
def test_member_names_with_backslashes_or_nul_are_refused(name):
    # zipfile itself rewrites these on some platforms, so the name check is tested directly
    with pytest.raises(UploadRejected):
        uv._check_member_name(name)


@pytest.mark.unit
@pytest.mark.parametrize(
    "mode", [stat.S_IFLNK | 0o777, stat.S_IFCHR | 0o666, stat.S_IFBLK | 0o660, stat.S_IFIFO | 0o644]
)
def test_zip_with_links_or_device_files_is_rejected(mode):
    assert (
        rejected("x.zip", make_zip({"ok.txt": b"fine", "bad": (b"/etc/passwd", mode)}))
        == "zip_special_file"
    )


@pytest.mark.unit
def test_encrypted_zip_is_rejected():
    data = bytearray(make_zip({"secret.txt": b"x" * 20}))
    struct.pack_into(
        "<H", data, data.rindex(b"PK\x01\x02") + 8, 0x1
    )  # general purpose flag: encrypted
    assert rejected("x.zip", bytes(data)) == "zip_encrypted"


@pytest.mark.unit
def test_zip_with_exotic_compression_is_rejected():
    assert (
        rejected("x.zip", make_zip({"a": b"x" * 100}, compress=zipfile.ZIP_BZIP2))
        == "zip_compression"
    )


@pytest.mark.unit
def test_duplicate_entries_are_rejected_case_insensitively():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", b"1")
        z.writestr("A.TXT", b"2")
    assert rejected("x.zip", buf.getvalue()) == "zip_duplicate_entry"


@pytest.mark.unit
def test_empty_zip_is_rejected():
    assert rejected("x.zip", make_zip({})) == "zip_empty"


@pytest.mark.unit
def test_too_many_entries_are_rejected():
    entries = {f"f{i}.txt": b"" for i in range(uv.MAX_ZIP_ENTRIES + 1)}
    assert rejected("x.zip", make_zip(entries)) == "zip_too_many_entries"


@pytest.mark.unit
def test_decompression_bomb_is_stopped_by_the_real_byte_count():
    # ~100 MiB of zeros compress to well under 1 MiB: far below the upload limits, far above ours.
    data = make_zip({"big.bin": b"\x00" * (100 * 1024 * 1024)})
    assert len(data) < 1024 * 1024
    assert rejected("x.zip", data) in ("zip_bomb", "zip_entry_too_large")


@pytest.mark.unit
def test_many_medium_entries_hit_the_total_limit():
    chunk = b"\x00" * (8 * 1024 * 1024)
    data = make_zip({f"p{i}.bin": chunk for i in range(10)})  # 80 MiB in total, 8 MiB each
    assert rejected("x.zip", data) == "zip_bomb"


@pytest.mark.unit
def test_a_header_that_lies_about_the_size_is_rejected():
    data = bytearray(make_zip({"a.txt": b"hello world" * 100}, compress=zipfile.ZIP_STORED))
    central = data.rindex(b"PK\x01\x02")
    struct.pack_into("<I", data, central + 24, 5)  # claimed uncompressed size
    assert rejected("x.zip", bytes(data)) in ("zip_corrupt", "zip_size_mismatch")


@pytest.mark.unit
def test_a_corrupt_entry_is_rejected():
    data = bytearray(make_zip({"a.txt": b"hello world" * 100}, compress=zipfile.ZIP_STORED))
    data[40] ^= 0xFF  # flip a byte of the stored content: the CRC no longer matches
    assert rejected("x.zip", bytes(data)) == "zip_corrupt"


def _overlapping_zip(count: int) -> bytes:
    """Several central-directory entries that all point at the same local entry."""
    payload = b"A" * 100_000
    name = b"a"
    local = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50,
        20,
        0,
        0,
        0,
        0x21,
        zlib.crc32(payload),
        len(payload),
        len(payload),
        1,
        0,
    )
    local += name + payload
    central = b""
    for i in range(count):
        n = bytes([ord("a") + i])
        central += (
            struct.pack(
                "<IHHHHHHIIIHHHHHII",
                0x02014B50,
                20,
                20,
                0,
                0,
                0,
                0x21,
                zlib.crc32(payload),
                len(payload),
                len(payload),
                1,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            + n
        )
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, count, count, len(central), len(local), 0)
    return local + central + end


@pytest.mark.unit
def test_overlapping_entries_are_rejected():
    assert rejected("x.zip", _overlapping_zip(5)) == "zip_overlapping_entries"


@pytest.mark.unit
def test_zip_content_under_another_name_is_a_mismatch():
    assert rejected("notes.txt", make_zip({"a": b"x"})) == "content_type_mismatch"


@pytest.mark.unit
def test_not_a_zip_with_a_zip_name_is_rejected():
    assert rejected("a.zip", b"this is not a zip" * 10) == "content_type_mismatch"
    assert rejected("a.zip", b"PK\x03\x04" + b"\x00" * 10) == "zip_invalid"


@pytest.mark.unit
def test_a_jar_is_inspected_like_any_zip_even_when_the_app_lists_jar():
    data = make_zip({"../evil.class": b"x"})
    assert rejected("lib.jar", data, ["jar"]) == "zip_path_traversal"
    assert inspect_upload("lib.jar", make_zip({"a.class": b"x"}), ["jar"]).kind == "zip"


# ----------------------------------------------------- other file types


@pytest.mark.unit
def test_pdf_images_and_text_pass_with_server_side_content_types():
    assert inspect_upload("a.pdf", PDF).content_type == "application/pdf"
    assert inspect_upload("a.png", PNG).content_type == "image/png"
    assert inspect_upload("a.JPG", JPEG).content_type == "image/jpeg"
    assert (
        inspect_upload("Aufgabe.java", "class Ä {}".encode("latin-1")).content_type == "text/plain"
    )
    assert (
        inspect_upload("x.unknown", b"\x01\x02\x03\xff").content_type == "application/octet-stream"
    )


@pytest.mark.unit
def test_claimed_type_must_match_the_bytes():
    assert rejected("a.pdf", PNG) == "content_type_mismatch"
    assert rejected("a.png", PDF) == "content_type_mismatch"
    assert rejected("a.jpg", PNG) == "content_type_mismatch"


@pytest.mark.unit
def test_text_files_must_be_text():
    assert rejected("a.csv", b"a,b\x00c\n") == "content_type_mismatch"
    assert rejected("a.java", PNG) == "content_type_mismatch"


@pytest.mark.unit
def test_programs_are_refused_by_content_and_by_name():
    assert rejected("a.txt", ELF) == "executable_not_allowed"
    assert rejected("harmless.pdf", ELF) == "executable_not_allowed"
    assert rejected("setup.exe", b"anything") == "executable_not_allowed"
    pe = bytearray(b"MZ" + b"\x00" * 0x7E)
    struct.pack_into("<I", pe, 0x3C, 0x40)
    pe[0x40:0x44] = b"PE\x00\x00"
    assert rejected("a.dat", bytes(pe)) == "executable_not_allowed"
    macho = b"\xcf\xfa\xed\xfe" + b"\x00" * 28
    assert rejected("a.dat", macho) == "executable_not_allowed"


@pytest.mark.unit
def test_a_program_passes_only_when_the_app_lists_its_extension():
    assert inspect_upload("setup.exe", ELF, ["exe"]).content_type == "application/octet-stream"
    assert rejected("setup.exe", ELF, ["zip"]) == "executable_not_allowed"


@pytest.mark.unit
def test_text_that_merely_starts_like_a_program_is_not_one():
    assert inspect_upload("a.csv", b"MZ,Name\n1,x\n").kind == "text"
    java_class = b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\x00" * 32
    assert inspect_upload("A.class", java_class).kind == "binary"


@pytest.mark.unit
def test_archive_formats_that_cannot_be_inspected_need_an_explicit_extension():
    gz = b"\x1f\x8b\x08\x00" + b"\x00" * 20
    assert rejected("a.tar.gz", gz) == "archive_not_inspectable"
    assert rejected("a.dat", gz) == "archive_not_inspectable"
    assert rejected("a.7z", b"7z\xbc\xaf\x27\x1c" + b"\x00" * 20) == "archive_not_inspectable"
    assert inspect_upload("a.tar.gz", gz, ["gz"]).kind == "gzip"


@pytest.mark.unit
def test_svg_with_script_or_external_references_is_rejected():
    clean = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8"/></svg>'
    assert inspect_upload("a.svg", clean).content_type == "image/svg+xml"
    for bad in (
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="x()"></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><a href="javascript:x()"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://evil.example/t.png"/></svg>',
        b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div/></foreignObject></svg>',
    ):
        assert rejected("a.svg", bad) == "svg_active_content"
    assert rejected("a.svg", b"plain text") == "svg_invalid"


# ------------------------------------------- integration with the upload path


def _payload(name: str, content: bytes, content_type: str = "text/html") -> DeploymentFileUpload:
    return DeploymentFileUpload(
        name=name,
        content_b64=base64.b64encode(content).decode("ascii"),
        size=len(content),
        content_type=content_type,
    )


ZIP_VAR = {
    "name": "assignments",
    "source": "terraform",
    "osType": "file",
    "osScope": "team",
    "varScope": "team",
    "fileExtensions": ["zip"],
}


@pytest.mark.unit
def test_upload_path_rejects_a_hostile_zip_with_a_structured_error():
    with pytest.raises(HTTPException) as exc:
        attach_files_to_user_input(
            {"terraform": {}, "packer": {}},
            {"assignments": {"team1": _payload("a.zip", make_zip({"../x": b"x"}))}},
            [ZIP_VAR],
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["reason"] == "file_content_rejected"
    assert exc.value.detail["code"] == "zip_path_traversal"
    assert exc.value.detail["variable"] == "assignments"
    assert exc.value.detail["filename"] == "a.zip"


@pytest.mark.unit
def test_upload_path_stores_the_content_type_derived_from_the_bytes():
    out = attach_files_to_user_input(
        {"terraform": {}, "packer": {}},
        {
            "assignments": {
                "team1": _payload("a.zip", make_zip({"ok.txt": b"x"}), content_type="text/html")
            }
        },
        [ZIP_VAR],
    )
    # a team-scoped variable is stored as {team: {"uploaded": file}}
    assert out["terraform"]["assignments"]["team1"]["uploaded"]["content_type"] == "application/zip"


@pytest.mark.unit
def test_upload_path_checks_content_even_without_an_extension_filter():
    var = {**ZIP_VAR, "fileExtensions": []}
    with pytest.raises(HTTPException) as exc:
        attach_files_to_user_input(
            {"terraform": {}, "packer": {}},
            {"assignments": {"team1": _payload("x.dat", ELF)}},
            [var],
        )
    assert exc.value.detail["code"] == "executable_not_allowed"


# ------------------------------------------------------------ app logo


def _data_url(mime: str, content: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


@pytest.mark.unit
def test_logo_must_be_an_allowed_type_with_matching_bytes():
    assert parse_image_data_url(_data_url("image/png", PNG))[1] == "image/png"
    for mime, data, reason in (
        ("image/png", PDF, "image_content_rejected"),
        ("image/jpeg", PNG, "image_content_rejected"),
        ("image/bmp", b"BM" + b"\x00" * 20, "unsupported_image_type"),
        (
            "image/svg+xml",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>',
            "image_content_rejected",
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            parse_image_data_url(_data_url(mime, data))
        assert exc.value.status_code == 422 and exc.value.detail["reason"] == reason
