"""Pod runtime: deployments and approvals record how a version runs

Revision ID: 8c4e6a0b2d5f
Revises: 7b3d5f9a2c4e
Create Date: 2026-10-15 13:00:00.000000

Linux apps run as pods rendered from their appstore.yaml (.github#4):

- ``deployments.runtime`` (``openstack-vm`` or ``kubernetes``),
  ``k8s_namespace`` (unique, deleted deployments included, so a destroy
  can never reach another deployment's namespace) and ``k8s_workloads``
  (for the owner's budget).
- ``app_version_approvals.runtime``, ``spec_sha256`` and ``image_digests``:
  what a review covered besides the commit.

Existing rows are VM deployments and VM reviews.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c4e6a0b2d5f"
down_revision: Union[str, None] = "7b3d5f9a2c4e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deployments", sa.Column("runtime", sa.String(length=16), server_default="openstack-vm", nullable=False)
    )
    op.add_column("deployments", sa.Column("k8s_namespace", sa.String(length=63), nullable=True))
    op.add_column("deployments", sa.Column("k8s_workloads", sa.Integer(), nullable=True))
    op.create_index(
        "uq_deployments_k8s_namespace",
        "deployments",
        ["k8s_namespace"],
        unique=True,
        postgresql_where=sa.text("k8s_namespace IS NOT NULL"),
    )
    op.add_column(
        "app_version_approvals",
        sa.Column("runtime", sa.String(length=16), server_default="openstack-vm", nullable=False),
    )
    op.add_column("app_version_approvals", sa.Column("spec_sha256", sa.String(length=64), nullable=True))
    op.add_column("app_version_approvals", sa.Column("image_digests", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("app_version_approvals", "image_digests")
    op.drop_column("app_version_approvals", "spec_sha256")
    op.drop_column("app_version_approvals", "runtime")
    op.drop_index("uq_deployments_k8s_namespace", table_name="deployments")
    op.drop_column("deployments", "k8s_workloads")
    op.drop_column("deployments", "k8s_namespace")
    op.drop_column("deployments", "runtime")
