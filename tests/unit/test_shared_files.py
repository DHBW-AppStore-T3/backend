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
    "k8s/kube.py": "54ea11c52aa97e57c14882ba4d15e5a3e1e4f21cc08548f4b7c3801b6068e1a6",
    "k8s/render.py": "255a8bc182fc39413c13e7424cec8c2b8308a6c14d36927ab13e977b6455572c",
    "k8s/spec.py": "8194f7d18e169e45072343844efa45d31a9044aa4606e4a6d299a4afb9695df2",
    "task_contract.py": "39a27e55e9b5ad0a76bdb5055a80b4b5541e6f929b906209e9cf6993f4409d5a",
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
