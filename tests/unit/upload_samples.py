"""Small valid files for the upload tests (built here, nothing downloaded)."""

from __future__ import annotations

import base64
import struct
import zlib


def png(width: int = 1, height: int = 1, trailing: bytes = b"") -> bytes:
    """A structurally valid PNG whose header announces ``width`` x ``height`` (one pixel of data)."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
        + trailing
    )


def jpeg(width: int = 1, height: int = 1, end: bool = True) -> bytes:
    """A JPEG with the segments of a baseline file (not decodable, but walkable)."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    sos = b"\xff\xda" + struct.pack(">H", 12) + b"\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    return b"\xff\xd8" + app0 + sof + sos + b"\x12\x34\x56\x78" + (b"\xff\xd9" if end else b"")


def gif(width: int = 1, height: int = 1, trailer: bool = True) -> bytes:
    return (
        b"GIF89a"
        + struct.pack("<HH", width, height)
        + b"\x80\x00\x00\x00\x00\x00\xff\xff\xff"
        + b"\x2c"
        + b"\x00" * 9
        + b"\x02\x01\x00\x00"
        + (b"\x3b" if trailer else b"")
    )


def webp() -> bytes:
    """A 1x1 lossless WebP."""
    return base64.b64decode("UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA==")


def pdf(extra_object: bytes = b"", objstm: bytes | None = None) -> bytes:
    """A minimal PDF; ``extra_object`` is appended as an object, ``objstm`` as a compressed object stream."""
    body = b"%PDF-1.5\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
    if extra_object:
        body += b"3 0 obj\n" + extra_object + b"\nendobj\n"
    if objstm is not None:
        packed = zlib.compress(objstm)
        body += (
            b"4 0 obj\n<< /Type /ObjStm /N 1 /First 4 /Filter /FlateDecode /Length "
            + str(len(packed)).encode()
            + b" >>\nstream\n"
            + packed
            + b"\nendstream\nendobj\n"
        )
    return body + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
