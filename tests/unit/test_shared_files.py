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
    "k8s/fake.py": "79ce1d3ae7ffa0c8c226ee91b1000ae49bee1355a85f0091e015e1bb4cf469ce",
    "k8s/kube.py": "c9c01d2dcef762709a374cf1a4518116e4a94433c869979809236546e78ec1cf",
    "k8s/render.py": "d12ed27fe15b50059980dd8a0b301c648f817dc0c4913a399d7a414e016a8428",
    "k8s/spec.py": "8a404dec68765e2beccfedba901e7437d17fe3f00d9b09fe460d6f26377a8701",
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
