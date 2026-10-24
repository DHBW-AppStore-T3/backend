"""File names of wizard uploads (.github#7 R, ported from dhbw-appstore).

An uploaded file's name travels on: VM apps typically use it as a path in
cloud-init ``write_files``, and the download route puts it into a
``Content-Disposition`` header. Both are injection points for a name like
``../../etc/cron.d/x`` or ``a"\\r\\nSet-Cookie: ...``, so the name is reduced
to a plain file name once, when the upload is accepted, and stored that way.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

MAX_LENGTH = 120
FALLBACK = "datei"

# Letters (any script), digits and a few harmless punctuation marks.
_UNSAFE = re.compile(r"[^\w.()+ -]", re.UNICODE)


def safe_filename(name: str | None) -> str:
    """A file name without directories, control characters or shell-hostile punctuation."""
    text = unicodedata.normalize("NFC", name or "")
    # Only the last path component, for either separator.
    text = re.split(r"[/\\]", text)[-1]
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = _UNSAFE.sub("_", text).strip(" .")
    if not text:
        return FALLBACK
    if len(text) > MAX_LENGTH:
        stem, dot, ext = text.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            text = stem[: MAX_LENGTH - len(ext) - 1] + "." + ext
        else:
            text = text[:MAX_LENGTH]
    return text


def content_disposition(name: str) -> str:
    """An ``attachment`` header value that is safe for any stored name (RFC 6266 / 5987)."""
    safe = safe_filename(name)
    ascii_fallback = safe.encode("ascii", "replace").decode("ascii").replace("?", "_")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(safe, safe='')}"
