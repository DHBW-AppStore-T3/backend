"""Report pod namespaces without a live deployment: ``python -m app.k8s_orphans``.

Every namespace the platform created carries its deployment's id. A namespace
whose deployment is gone, hidden or destroyed, or that is not the namespace
the deployment row names, keeps running and costs cluster capacity with
nobody seeing it in the AppStore (a destroy that failed for good, a manual
change). This lists them, with exit code 1 if there are any; deleting them
stays a decision for an operator.

Needs a Kubernetes identity that may list namespaces (in-cluster service
account or ``KUBECONFIG``).
"""

from __future__ import annotations

import sys
import uuid

from app.database import SessionLocal
from app.k8s.kube import KubeApi, managed_selector
from app.k8s.render import DEPLOYMENT_LABEL
from app.models import RUNTIME_K8S, Deployment, Task, TaskStatus, TaskType


def _live(db, deployment_id: str, namespace: str) -> bool:
    try:
        dep_uuid = uuid.UUID(deployment_id)
    except ValueError:
        return False
    dep = db.get(Deployment, dep_uuid)
    if dep is None or dep.deleted_at is not None or dep.runtime != RUNTIME_K8S or dep.k8s_namespace != namespace:
        return False
    destroyed = (
        db.query(Task.taskId)
        .filter(Task.deploymentId == dep_uuid, Task.type == TaskType.DESTROY, Task.status == TaskStatus.SUCCESS)
        .first()
    )
    return destroyed is None


def find_orphans(kube, db) -> list[tuple[str, str]]:
    """``[(namespace, deployment id label)]`` of managed namespaces without a live deployment."""
    orphans = []
    for namespace in sorted(kube.namespaces(managed_selector())):
        snap = kube.snapshot(namespace)
        if not snap.exists or snap.terminating:
            continue
        deployment_id = snap.labels.get(DEPLOYMENT_LABEL, "")
        if not _live(db, deployment_id, namespace):
            orphans.append((namespace, deployment_id))
    return orphans


def main() -> int:
    with SessionLocal() as db:
        orphans = find_orphans(KubeApi(), db)
    for namespace, deployment_id in orphans:
        print(f"{namespace}: no live deployment (label {deployment_id or 'missing'})")
    print(f"{len(orphans)} namespace(s) without a live deployment")
    return 1 if orphans else 0


if __name__ == "__main__":
    sys.exit(main())
