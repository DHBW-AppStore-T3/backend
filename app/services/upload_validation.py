"""Content checks for files a person attaches to a deployment (wizard uploads).

``deployment_input.attach_files_to_user_input`` already checks name, extension,
base64, declared size and the size caps. This module looks at the *content*,
because an upload ends up in somebody's pod or VM: a ZIP is unpacked there, a
PDF or image is opened there. The rules, in order of importance:

* **Nothing is trusted that the file says about itself.** The type comes from the
  bytes (magic numbers), not from the name or the browser's content type, and
  the sizes in a ZIP's headers are only compared with what actually comes out
  of the decompressor.
* **ZIPs are inspected completely.** Every entry is decompressed once into a
  void, with hard limits on the number of entries and on the real number of
  bytes (decompression bombs, overlapping entries and lying headers all end
  here), and the CRC of every entry is verified. Entries with absolute or
  ``..`` paths, backslashes, control characters, symbolic links or device
  files, encrypted entries and exotic compression methods reject the whole
  archive: rejecting beats guessing what an unpacker would do with them.
* **A claimed type must match.** ``x.pdf`` has to be a PDF, ``x.zip`` a ZIP, and
  so on; text-like files must be text (no NUL bytes).
* **Programs are refused** (ELF, PE, Mach-O, whatever the file is called), and so
  are archive formats this module cannot look into (gzip, tar, 7z, rar, ...),
  unless the app author listed that extension explicitly in the app's
  ``ext``/``fileExtensions``. A file that is allowed through that way is still
  opaque: this module then only knows that it is what it claims to be.

The checks are plain functions on ``bytes`` (no I/O, no database), so they are
cheap to test and safe to call from a request handler: the work is bounded by
:data:`MAX_ZIP_TOTAL_BYTES` however hostile the input is.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from dataclasses import dataclass

# ---------------------------------------------------------------- limits

MAX_ZIP_ENTRIES = 2000
MAX_ZIP_ENTRY_BYTES = 32 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ZIP_PATH_LENGTH = 1024
MAX_ZIP_COMPONENT_LENGTH = 255
MAX_ZIP_DEPTH = 32
_CHUNK = 64 * 1024

# --------------------------------------------------------------- the model


class UploadRejected(ValueError):
    """The content of an upload is not acceptable. ``code`` is stable and machine readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Inspected:
    """What the checks found out: a server-side content type and, for a ZIP, its size."""

    kind: str
    content_type: str
    zip_entries: int = 0
    zip_unpacked_bytes: int = 0


# ------------------------------------------------------------ type tables

_KIND_MIME = {
    "zip": "application/zip",
    "pdf": "application/pdf",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "text": "text/plain",
}

# Which extensions claim which kind. A claimed kind must be what the bytes say.
_EXT_KIND = {
    "zip": "zip",
    "pdf": "pdf",
    "png": "png",
    "jpg": "jpeg",
    "jpeg": "jpeg",
    "gif": "gif",
    "webp": "webp",
    "svg": "svg",
}

# Text-like files: any bytes without NUL and without control characters other than
# TAB, LF, CR and FF. Any 8-bit encoding passes (UTF-8, Latin-1, cp1252), because
# lecturers do upload German source files from Windows.
_TEXT_EXTS = frozenset(
    [
        "txt",
        "csv",
        "tsv",
        "md",
        "rst",
        "java",
        "py",
        "c",
        "h",
        "cc",
        "cpp",
        "hpp",
        "cs",
        "js",
        "mjs",
        "ts",
        "tsx",
        "jsx",
        "json",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "conf",
        "properties",
        "sql",
        "tex",
        "bib",
        "sty",
        "cls",
        "log",
        "xml",
        "html",
        "htm",
        "css",
        "sh",
        "bash",
        "bat",
        "ps1",
        "r",
        "m",
        "ipynb",
        "gradle",
        "kt",
        "scala",
        "go",
        "rs",
        "php",
        "rb",
        "pl",
        "lua",
        "swift",
        "lic",
    ]
)

# Programs. Allowed only if the app author listed the extension explicitly.
_EXECUTABLE_EXTS = frozenset(
    ["exe", "dll", "msi", "so", "dylib", "bin", "run", "elf", "com", "scr"]
)

# Archive formats that this module cannot look into.
_OPAQUE_ARCHIVE_EXTS = frozenset(
    [
        "gz",
        "tgz",
        "bz2",
        "xz",
        "zst",
        "lz",
        "7z",
        "rar",
        "tar",
        "cab",
        "iso",
        "img",
        "jar",
        "war",
        "ear",
        "apk",
    ]
)

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06")  # normal, and the empty-archive end record


def _sniff(data: bytes) -> str:
    """The kind of a file by its first bytes ("text"/"binary" if it is none of the known ones)."""
    head = data[:16]
    if head.startswith(_ZIP_MAGICS):
        return "zip"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"\x7fELF"):
        return "elf"
    if head.startswith(b"MZ") and _is_pe(data):
        return "pe"
    if head[:4] in (
        b"\xfe\xed\xfa\xce",
        b"\xfe\xed\xfa\xcf",
        b"\xce\xfa\xed\xfe",
        b"\xcf\xfa\xed\xfe",
    ):
        return "macho"
    # 0xCAFEBABE is a universal Mach-O binary (a small architecture count follows) and also a Java class
    # file (a version number >= 45 follows).
    if head[:4] == b"\xca\xfe\xba\xbe" and int.from_bytes(head[4:8], "big") < 20:
        return "macho"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head.startswith(b"BZh"):
        return "bzip2"
    if head.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if head.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "rar"
    if head.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd"
    if len(data) > 262 and data[257:262] == b"ustar":
        return "tar"
    return "text" if _looks_like_text(data) else "binary"


def _is_pe(data: bytes) -> bool:
    """A DOS header whose e_lfanew points at a PE signature (not just a text that starts with "MZ")."""
    if len(data) < 0x40:
        return False
    offset = int.from_bytes(data[0x3C:0x40], "little")
    return offset + 4 <= len(data) and data[offset : offset + 4] == b"PE\x00\x00"


_BAD_TEXT_BYTES = re.compile(rb"[\x00-\x08\x0b\x0e-\x1f]")


def _looks_like_text(data: bytes) -> bool:
    return _BAD_TEXT_BYTES.search(data) is None


_EXECUTABLE_KINDS = frozenset({"elf", "pe", "macho"})
_OPAQUE_KINDS = frozenset({"gzip", "bzip2", "xz", "7z", "rar", "zstd", "tar"})


# ---------------------------------------------------------------- SVG

_SVG_ACTIVE = re.compile(
    rb"<\s*script|<\s*foreignObject|<\s*iframe|<\s*embed|<\s*object|<!\s*ENTITY|<!\s*DOCTYPE[^>]*\[|"
    rb"\bon[a-z]+\s*=|javascript\s*:|data\s*:\s*text/html|(?:xlink:)?href\s*=\s*[\"']?\s*(?:https?:|//|file:)",
    re.IGNORECASE,
)


def check_svg(data: bytes) -> None:
    """Reject SVG that can run script or load something from elsewhere (XSS, XXE, tracking)."""
    if not _looks_like_text(data) or b"<svg" not in data[:4096].lower():
        raise UploadRejected("svg_invalid", "the file is not an SVG image")
    if _SVG_ACTIVE.search(data):
        raise UploadRejected(
            "svg_active_content", "the SVG contains script, entities or external references"
        )


# ---------------------------------------------------------------- ZIP


def _check_member_name(name: str) -> None:
    if not name or len(name) > MAX_ZIP_PATH_LENGTH:
        raise UploadRejected("zip_bad_name", "an entry has an empty or too long path")
    if "\x00" in name or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise UploadRejected("zip_bad_name", "an entry path contains control characters")
    if "\\" in name:
        raise UploadRejected("zip_bad_name", "an entry path contains a backslash")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise UploadRejected("zip_absolute_path", "an entry has an absolute path")
    parts = name.rstrip("/").split("/")
    if len(parts) > MAX_ZIP_DEPTH:
        raise UploadRejected("zip_bad_name", "an entry path is nested too deeply")
    for part in parts:
        if part in ("", ".", ".."):
            raise UploadRejected(
                "zip_path_traversal", "an entry path contains '.', '..' or an empty component"
            )
        if len(part) > MAX_ZIP_COMPONENT_LENGTH:
            raise UploadRejected("zip_bad_name", "an entry path component is too long")


def inspect_zip(data: bytes) -> tuple[int, int]:
    """Check a ZIP completely. Returns ``(entries, unpacked_bytes)``; raises :class:`UploadRejected`."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise UploadRejected(
            "zip_invalid", f"not a readable ZIP archive ({exc.__class__.__name__})"
        ) from None
    with archive:
        infos = archive.infolist()
        if not infos:
            raise UploadRejected("zip_empty", "the ZIP archive is empty")
        if len(infos) > MAX_ZIP_ENTRIES:
            raise UploadRejected("zip_too_many_entries", f"more than {MAX_ZIP_ENTRIES} entries")

        seen: set[str] = set()
        compressed_total = 0
        for info in infos:
            _check_member_name(info.filename)
            folded = info.filename.rstrip("/").casefold()
            if folded in seen:
                raise UploadRejected("zip_duplicate_entry", "two entries have the same path")
            seen.add(folded)
            if info.flag_bits & 0x1:
                raise UploadRejected("zip_encrypted", "the archive has encrypted entries")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise UploadRejected(
                    "zip_compression", "the archive uses an unsupported compression method"
                )
            mode = info.external_attr >> 16 if info.create_system == 3 else 0
            if mode and (
                stat.S_ISLNK(mode)
                or stat.S_ISCHR(mode)
                or stat.S_ISBLK(mode)
                or stat.S_ISFIFO(mode)
                or stat.S_ISSOCK(mode)
            ):
                raise UploadRejected(
                    "zip_special_file", "the archive contains a symbolic link or a device file"
                )
            if info.is_dir() and info.file_size:
                raise UploadRejected("zip_bad_name", "a directory entry has content")
            if info.file_size > MAX_ZIP_ENTRY_BYTES:
                raise UploadRejected("zip_entry_too_large", "an entry is larger than the limit")
            compressed_total += info.compress_size

        # Entries that share compressed bytes (the "quine"/overlapping-entry bomb) add up to more
        # than the archive is long.
        if compressed_total > len(data):
            raise UploadRejected("zip_overlapping_entries", "the archive's entries overlap")

        unpacked = 0
        for info in infos:
            if info.is_dir():
                continue
            entry_bytes = 0
            try:
                with archive.open(info) as handle:
                    while True:
                        chunk = handle.read(_CHUNK)
                        if not chunk:
                            break
                        entry_bytes += len(chunk)
                        unpacked += len(chunk)
                        # The real number of bytes counts, not the headers' claim.
                        if entry_bytes > MAX_ZIP_ENTRY_BYTES or unpacked > MAX_ZIP_TOTAL_BYTES:
                            raise UploadRejected(
                                "zip_bomb", "the archive unpacks to more than the limit"
                            )
            except UploadRejected:
                raise
            except (
                zipfile.BadZipFile,
                NotImplementedError,
                RuntimeError,
                EOFError,
                OSError,
                ValueError,
            ) as exc:
                raise UploadRejected(
                    "zip_corrupt", f"an entry cannot be read ({exc.__class__.__name__})"
                ) from None
            if entry_bytes != info.file_size:
                raise UploadRejected(
                    "zip_size_mismatch", "an entry's real size differs from its header"
                )
        return len(infos), unpacked


# --------------------------------------------------------------- the API


def _extension(name: str) -> str:
    base = (name or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = base.rfind(".")
    return base[dot + 1 :].lower() if dot > 0 else ""


def inspect_upload(name: str, data: bytes, declared_exts: list[str] | None = None) -> Inspected:
    """Check one uploaded file. Raises :class:`UploadRejected`; otherwise returns what it is.

    ``declared_exts`` are the extensions the app author allows for this variable (lowercase, no
    dot), or ``None`` if the app did not restrict the extension.
    """
    ext = _extension(name)
    explicit = {e.lower().lstrip(".") for e in (declared_exts or [])}
    kind = _sniff(data)

    if kind in _EXECUTABLE_KINDS or ext in _EXECUTABLE_EXTS:
        if ext not in explicit or ext not in _EXECUTABLE_EXTS:
            raise UploadRejected(
                "executable_not_allowed", "programs cannot be uploaded to this variable"
            )
        return Inspected(kind, "application/octet-stream")

    # (a .jar/.war is a ZIP and gets the full ZIP inspection below, whatever it is called)
    if kind in _OPAQUE_KINDS or (ext in _OPAQUE_ARCHIVE_EXTS and kind != "zip"):
        if ext not in explicit:
            raise UploadRejected(
                "archive_not_inspectable",
                "this archive format cannot be checked; only ZIP is accepted unless the app lists the extension",
            )
        return Inspected(kind if kind in _OPAQUE_KINDS else "binary", "application/octet-stream")

    claimed = _EXT_KIND.get(ext)
    if claimed and claimed != kind and not (claimed == "svg" and kind == "text"):
        raise UploadRejected("content_type_mismatch", f"the content is not a {ext.upper()} file")
    if ext in _TEXT_EXTS and kind not in ("text",):
        raise UploadRejected("content_type_mismatch", f"the content of a .{ext} file must be text")

    if kind == "zip":
        entries, unpacked = inspect_zip(data)
        return Inspected("zip", _KIND_MIME["zip"], entries, unpacked)
    if claimed == "svg":
        check_svg(data)
        return Inspected("svg", _KIND_MIME["svg"])
    if kind == "pdf":
        return Inspected("pdf", _KIND_MIME["pdf"])
    if kind in ("png", "jpeg", "gif", "webp"):
        return Inspected(kind, _KIND_MIME[kind])
    if kind == "text":
        return Inspected("text", _KIND_MIME["text"])
    return Inspected("binary", "application/octet-stream")
