"""Code shared with the worker stays identical (.github#5).

``app/pgq.py`` (the Kombu transport), ``app/task_contract.py`` (event
types, payloads, sealed results), ``app/git_urls.py`` (the Git host
allowlist, .github#7 B), ``app/keystone_urls.py`` (the Keystone URL
allowlist, .github#7 C) and ``app/k8s/`` (the pod contract, renderer and
cluster access, .github#4) exist in ``backend`` and ``worker`` until a shared
package is decided. The same test in both repositories pins their
content: change a file in one repository, copy it to the other and update
the hash in both tests.
"""

import hashlib
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"

SHARED = {
    "git_urls.py": "c9b06a9fd9fc28b226e365eb4f90aa002a0205c11d2ba2abcda99b2e95258583",
    "keystone_urls.py": "a1f8533680323af3e943a98a3fe27812b2e2501484a30516309bb00d2d2a254f",
    "k8s/__init__.py": "500314b04df7e61475d0e29efb5c22a3b7bd2d4b6df7f0ebf3ab55f150d53274",
    "k8s/fake.py": "14ce65fc926c5770bd3792ce996e178417af102703c957e36feeee6da4eb924d",
    "k8s/kube.py": "09d86caac0d3a1875292dd1382edcaf294d6da6f696d9001458277d7f57206f5",
    "k8s/render.py": "d3f2bf1f716f26a89f664b30695faf6b5f664c845b7663134ae19b2ba96ad954",
    "k8s/spec.py": "cce17aef552de19205eb016d37e96920a371fd450296a957b1ccc57275634e99",
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
