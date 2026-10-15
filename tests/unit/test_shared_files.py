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
    "k8s/fake.py": "53bdc07a446b76847a4501556c486db917639a542088ade050b755ac8eecbb9d",
    "k8s/kube.py": "b0d6d27cf1dbdc41b09c9b59c66e35cab33c36b0156b590568a4e99edd6719e9",
    "k8s/render.py": "cc2890bdd63b000ae23c0bb15e4395e99b392bbf87665f4df72f1a47537cfb99",
    "k8s/spec.py": "3637290b5d6f7587c18a2bcd6635c78e66f191136097ea08ffc35bc470deafe9",
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
