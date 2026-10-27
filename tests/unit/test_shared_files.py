"""Code shared with the worker stays identical (.github#5).

``app/task_contract.py`` (queue, event types, payloads, sealed
results), ``app/git_urls.py`` (the Git host
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
    "git_urls.py": "d0daeea0c6645fce1593941dc2f74ba66fbc79af79214a2eb0a6dd9b2cbe8a5d",
    "k8s/__init__.py": "500314b04df7e61475d0e29efb5c22a3b7bd2d4b6df7f0ebf3ab55f150d53274",
    "k8s/fake.py": "c032151238b66e3ad4f04e4853f21b04112e064f7087dd5a0082977b01782306",
    "k8s/kube.py": "6fb11274b452c179a18ea2adbc35a8d94c2c401e4bd7cb0d0404780b2bf2afc4",
    "k8s/render.py": "0fae1f8ecc9319d7d60858ba7b6df71d9a91cb317d905a2a3c3fc6278873d593",
    "k8s/spec.py": "d1c05b57aca2641806af3fe393e95a587319da44ec8099567b594dfd636901bc",
    "task_contract.py": "ef5105e6398a7a559886001f0e27fe221fb5d1eb50567d8d28e39ccee96c5622",
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
