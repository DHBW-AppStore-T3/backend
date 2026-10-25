"""Structure checks for PDF and image uploads (stdlib only; used by ``upload_validation``).

A magic number only says what a file claims to be. These checks look inside, within hard limits:

* **PDF**: encrypted PDFs, active content (JavaScript, launch actions, embedded files, rich media,
  XFA forms, form submission) and object streams that cannot be read are refused. Names are
  normalised first (``/J#61vaScript`` is ``/JavaScript``), and the compressed object streams, where
  PDF 1.5+ keeps its dictionaries, are inflated within a byte budget and scanned too. Stream data
  of images and fonts is not scanned: random bytes would produce false alarms.
* **PNG / JPEG / GIF / WebP**: the container is walked (PNG: every chunk and its CRC), the pixel
  count is limited (a small file can announce a huge image: decompression bomb), and data appended
  after the end of a PNG or GIF (a polyglot with an archive) is refused.

Everything raises :class:`FormatRejected`; ``upload_validation`` turns it into its own error.
"""

from __future__ import annotations

import re
import struct
import zlib

MAX_PIXELS = 100_000_000
MAX_PDF_INFLATE_PER_STREAM = 8 * 1024 * 1024
MAX_PDF_INFLATE_TOTAL = 32 * 1024 * 1024


class FormatRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- PDF

_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
# A PDF name ends at a delimiter or white space. Only names that start something active.
_ACTIVE_NAMES = re.compile(
    rb"/(JavaScript|JS|Launch|EmbeddedFile|EmbeddedFiles|RichMedia|XFA|SubmitForm|ImportData|GoToE)(?=[\s/\[\]<>()%]|$)"
)
_ENCRYPT = re.compile(rb"/Encrypt(?=[\s/\[\]<>()%]|$)")
_OBJSTM = re.compile(rb"/Type\s*/ObjStm(?=[\s/\[\]<>()%]|$)")
# "endstream" ends with "stream" too: only a keyword that does not follow "end" starts a stream.
_STREAM_START = re.compile(rb"(?<!end)stream(?:\r\n|\n|\r)")


def _normalise(chunk: bytes) -> bytes:
    return _NAME_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), chunk)


def _scan_text(chunk: bytes) -> None:
    text = _normalise(chunk)
    if _ENCRYPT.search(text):
        raise FormatRejected("pdf_encrypted", "the PDF is encrypted")
    found = _ACTIVE_NAMES.search(text)
    if found:
        raise FormatRejected(
            "pdf_active_content",
            f"the PDF contains active or embedded content (/{found.group(1).decode('ascii')})",
        )


def inspect_pdf(data: bytes) -> None:
    """Refuse encrypted PDFs, active content and unreadable object streams."""
    position = 0
    inflated_total = 0
    outside: list[bytes] = []
    while True:
        start = _STREAM_START.search(data, position)
        if not start:
            outside.append(data[position:])
            break
        outside.append(data[position : start.start()])
        end = data.find(b"endstream", start.end())
        if end < 0:
            raise FormatRejected("pdf_invalid", "a PDF stream has no end")
        # The dictionary of this stream is the tail of the text in front of it.
        dictionary = _normalise(data[max(0, start.start() - 2048) : start.start()])
        dictionary = (
            dictionary[dictionary.rfind(b"obj") + 3 :] if b"obj" in dictionary else dictionary
        )
        if _OBJSTM.search(dictionary):
            body = data[start.end() : end]
            decoder = zlib.decompressobj()
            try:
                text = decoder.decompress(body, MAX_PDF_INFLATE_PER_STREAM)
            except zlib.error:
                raise FormatRejected(
                    "pdf_unscannable", "an object stream cannot be decompressed"
                ) from None
            if decoder.unconsumed_tail:
                raise FormatRejected("pdf_unscannable", "an object stream is larger than the limit")
            inflated_total += len(text)
            if inflated_total > MAX_PDF_INFLATE_TOTAL:
                raise FormatRejected(
                    "pdf_unscannable", "the object streams are larger than the limit"
                )
            _scan_text(text)
        position = end + len(b"endstream")
    # Dictionaries, trailer and cross-reference data: the text between the streams.
    for chunk in outside:
        _scan_text(chunk)


# ---------------------------------------------------------------- images


def _check_pixels(width: int, height: int) -> None:
    if width < 1 or height < 1:
        raise FormatRejected("image_invalid", "the image has no size")
    if width * height > MAX_PIXELS:
        raise FormatRejected("image_too_large", "the image has more than 100 million pixels")


def inspect_png(data: bytes) -> None:
    position = 8
    seen_ihdr = False
    while True:
        if position + 12 > len(data):
            raise FormatRejected("image_invalid", "the PNG is truncated")
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        end = position + 8 + length + 4
        if end > len(data):
            raise FormatRejected("image_invalid", "the PNG is truncated")
        body = data[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", data[end - 4 : end])
        if zlib.crc32(kind + body) != crc:
            raise FormatRejected("image_invalid", "a PNG chunk is damaged")
        if not seen_ihdr:
            if kind != b"IHDR" or length != 13:
                raise FormatRejected("image_invalid", "the PNG has no header chunk")
            width, height = struct.unpack(">II", body[:8])
            _check_pixels(width, height)
            seen_ihdr = True
        if kind == b"IEND":
            if end != len(data):
                raise FormatRejected(
                    "image_trailing_data", "there is data after the end of the PNG"
                )
            return
        position = end


def inspect_jpeg(data: bytes) -> None:
    position = 2
    size = None
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            raise FormatRejected("image_invalid", "the JPEG has a broken segment")
        marker = data[position + 1]
        if marker == 0xFF:  # fill byte
            position += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            position += 2
            continue
        if marker == 0xD9:
            break
        (length,) = struct.unpack(">H", data[position + 2 : position + 4])
        if length < 2 or position + 2 + length > len(data):
            raise FormatRejected("image_invalid", "the JPEG is truncated")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if length < 8:
                raise FormatRejected("image_invalid", "the JPEG frame header is too short")
            height, width = struct.unpack(">HH", data[position + 5 : position + 9])
            size = (width, height)
        if marker == 0xDA:  # start of scan: entropy-coded data follows; the size is known by now
            break
        position += 2 + length
    if size is None:
        raise FormatRejected("image_invalid", "the JPEG has no frame header")
    _check_pixels(*size)
    if b"\xff\xd9" not in data[-4096:]:
        raise FormatRejected("image_invalid", "the JPEG has no end marker")


def inspect_gif(data: bytes) -> None:
    if len(data) < 14:
        raise FormatRejected("image_invalid", "the GIF is truncated")
    width, height = struct.unpack("<HH", data[6:10])
    _check_pixels(width, height)
    if data[-1:] != b"\x3b":
        raise FormatRejected("image_trailing_data", "the GIF does not end with its trailer")


def inspect_webp(data: bytes) -> None:
    if len(data) < 30:
        raise FormatRejected("image_invalid", "the WebP is truncated")
    (riff_size,) = struct.unpack("<I", data[4:8])
    if riff_size + 8 != len(data) and riff_size + 9 != len(data):  # RIFF pads to an even length
        raise FormatRejected("image_trailing_data", "the WebP's size does not match the file")
    kind = data[12:16]
    if kind == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
    elif kind == b"VP8L":
        if data[20] != 0x2F:
            raise FormatRejected("image_invalid", "the WebP header is damaged")
        bits = int.from_bytes(data[21:25], "little")
        width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    elif kind == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise FormatRejected("image_invalid", "the WebP header is damaged")
        width, height = struct.unpack("<HH", data[26:30])
        width, height = width & 0x3FFF, height & 0x3FFF
    else:
        raise FormatRejected("image_invalid", "unknown WebP content")
    _check_pixels(width, height)
