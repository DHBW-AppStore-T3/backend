"""Code shared with the worker stays identical (.github#5).

``app/pgq.py`` (the Kombu transport), ``app/task_contract.py`` (event
types, payloads, sealed results), ``app/git_urls.py`` (the Git host
allowlist, .github#7 B) and ``app/k8s/`` (the pod contract, renderer and
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
    "k8s/__init__.py": "500314b04df7e61475d0e29efb5c22a3b7bd2d4b6df7f0ebf3ab55f150d53274",
    "k8s/fake.py": "7d4068cd2a97b14e19180090aa5ffe40b243012f0a8f3e46a7105d556e6cdd77",
    "k8s/kube.py": "1544b0f3a8dd10b8fed7cb1c41e653322592d499f36ed9f74f13a62be6396610",
    "k8s/render.py": "f2b9e9e8075dbe8c450faf854073a86d83a68835a11ebfcfb7c6b8307f2f40e0",
    "k8s/spec.py": "d1c05b57aca2641806af3fe393e95a587319da44ec8099567b594dfd636901bc",
    "pgq.py": "57f323480984fbad424662810cfec1ebc39256fa2ec7b77680863b1f416b62e4",
    "task_contract.py": "009fa263871d74a0cf020033f13d81543e971b278d4a80ba3d35b8983940afd4",
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(SHARED))
def test_shared_file_matches_the_other_repository(name):
    content = (APP / name).read_bytes().replace(b"\r\n", b"\n")
    actual = hashlib.sha256(content).hexdigest()
    assert actual == SHARED[name], (
        f"app/{name} differs from the version shared with the worker (sha256 {actual}, pinned {SHARED[name]}). "
        f"Changed it on purpose? Copy the file to worker/app/{name} and put the new hash into SHARED of "
        "tests/unit/test_shared_files.py in BOTH repositories. Both services must roll out together "
        "(the task contract is live between them). Otherwise restore the file."
    )
