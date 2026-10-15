"""Git URLs of app repositories, checked the same way in the API and the worker.

App repositories are public and read anonymously (.github#7 B): there is no
platform Git token any more that a registered URL could lure to another
server. Both sides turn whatever the app author registered into the same
credential-free HTTPS URL and refuse every host outside
``APP_GIT_ALLOWED_HOSTS``: the URL is user input, and without the allowlist
the API and the worker would fetch from wherever it points, internal
addresses included.

This file exists identically in ``backend`` and ``worker``; a test in both
repositories pins its hash (as for ``pgq.py``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# owner/repo(.git), possibly nested groups (GitLab). Only plain segments:
# the URL ends up as an argument to git, so nothing option-like, no
# traversal, no query or fragment.
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.~-]*"
_PATH_RE = re.compile(rf"^{_SEGMENT}(/{_SEGMENT})+/?$")
# A DNS name; a port, userinfo or an IP literal never matches an allowlist
# entry, which is checked separately and exactly.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
# A full commit SHA as git prints it.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitHostNotAllowed(ValueError):
    """The repository URL is malformed or points outside the allowlist."""


def to_https_url(git_url: str) -> str:
    """Normalise ``git@host:owner/repo`` and ``http(s)://…`` to plain ``https://``.

    Credentials embedded in the URL are dropped: nothing is cloned with a
    token, so a user-supplied one must not be passed on either.
    """
    url = git_url.strip()
    if url.startswith("git@"):
        url = url[len("git@"):].replace(":", "/", 1)
    url = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    host_and_path = url.split("/", 1)
    if "@" in host_and_path[0]:
        host_and_path[0] = host_and_path[0].rsplit("@", 1)[1]
    return "https://" + "/".join(host_and_path)


def parse_allowed_hosts(value: str) -> frozenset[str]:
    """``APP_GIT_ALLOWED_HOSTS`` (comma-separated) as a set of lower-case hosts."""
    return frozenset(h.strip().lower() for h in value.split(",") if h.strip())


def checked_https_url(git_url: str, allowed_hosts: Iterable[str]) -> str:
    """:func:`to_https_url`, refusing anything but an allowed host and a plain path.

    Raises :class:`GitHostNotAllowed`. The result is what gets fetched and
    what the API stores as the app's ``git_link``.
    """
    if not isinstance(git_url, str) or not git_url.strip():
        raise GitHostNotAllowed("no repository URL")
    url = to_https_url(git_url)
    host, _, path = url[len("https://"):].partition("/")
    host = host.lower()
    if not _HOST_RE.match(host) or ".." in path or not _PATH_RE.match(path):
        raise GitHostNotAllowed(f"not a repository URL: {git_url!r}")
    if host not in set(allowed_hosts):
        raise GitHostNotAllowed(f"Git host {host!r} is not allowed")
    return f"https://{host}/{path.rstrip('/')}"


def is_commit_sha(value: object) -> bool:
    """Whether ``value`` is a full 40-character lower-case commit SHA."""
    return isinstance(value, str) and bool(_SHA_RE.match(value))


__all__ = [
    "GitHostNotAllowed",
    "checked_https_url",
    "is_commit_sha",
    "parse_allowed_hosts",
    "to_https_url",
]
