"""Code shared with the worker stays identical (.github#5).

``app/pgq.py`` (the Kombu transport), ``app/task_contract.py`` (event
types, payloads, sealed results), ``app/git_urls.py`` (the Git host
allowlist, .github#7 B) and ``app/keystone_urls.py`` (the Keystone URL
allowlist, .github#7 C) exist in ``backend`` and ``worker`` until a shared
package is decided. The same test in both repositories pins their
content: change a file in one repository, copy it to the other and update
the hash in both tests.
"""

import hashlib
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

SHARED = {
    "git_urls.py": "5d1ce51a809dcf4d17afa2e7e79b23f138a4637e9ba49162d4c053d95dcb7948",
    "keystone_urls.py": "a1f8533680323af3e943a98a3fe27812b2e2501484a30516309bb00d2d2a254f",
    "pgq.py": "57f323480984fbad424662810cfec1ebc39256fa2ec7b77680863b1f416b62e4",
    "task_contract.py": "4c77d4e3d5e986ab61bf3eb497a34f34c27c97881522660d2871b1faf58a7ad1",
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(SHARED))
def test_shared_file_matches_the_other_repository(name):
    content = (APP / name).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(content).hexdigest() == SHARED[name], (
        f"app/{name} differs from the version shared with the worker; "
        "keep both copies identical and update the hash in both tests"
    )
