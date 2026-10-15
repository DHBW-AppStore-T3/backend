"""VM path: OpenTofu state behind the API, credentials per project

Revision ID: 7b3d5f9a2c4e
Revises: 6a2c4e8b1d3f
Create Date: 2026-10-15 11:00:00.000000

The VM path (Windows apps) follows dhbw-appstore:

- ``terraform_states``: the OpenTofu state of each VM deployment, Fernet-
  encrypted, with OpenTofu's lock. OpenTofu reaches it through the API's
  HTTP state backend; the job no longer gets a database login that opened
  every deployment's state.
- ``tasks.state_token_hash``: the hash of the per-task token for that
  backend (the token travels sealed in the job message).
- One OpenStack credential per user and project (was: one per user), the
  project as Keystone scoped the token (``project_verified``).
  ``deployments.os_project_id`` records the project a VM deployment lives
  in; everyone with a verified credential for it is a peer.

- ``worker_live_deployments``: app and project of every live deployment,
  the only thing of ``deployments`` the worker role may read. A destroy
  deletes the deployment's Packer images only when no other live
  deployment of the same app in the project may still boot from them.

Existing credentials keep their data but are *not* verified: their
``project_id`` is what the user typed (an application credential never
even used it), and trusting it would let anyone who typed someone else's
project id become a peer of that project's deployments. Existing
deployments therefore get no ``os_project_id`` here. Both are filled when
the owner re-checks the old credential: Keystone names the project, and
since a credential could not be changed while its owner had live
deployments, those deployments live in exactly that project.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

WORKER_ROLE = "appstore_worker"

# revision identifiers, used by Alembic.
revision: str = "7b3d5f9a2c4e"
down_revision: Union[str, None] = "6a2c4e8b1d3f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "terraform_states",
        sa.Column("deploymentId", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.LargeBinary(), nullable=True),
        sa.Column("lock_id", sa.String(), nullable=True),
        sa.Column("lock_info", sa.Text(), nullable=True),
        sa.Column("lock_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["deploymentId"], ["deployments.deploymentId"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("deploymentId"),
    )
    op.add_column("tasks", sa.Column("state_token_hash", sa.String(length=64), nullable=True))

    op.add_column("deployments", sa.Column("os_project_id", sa.String(), nullable=True))
    op.create_index(op.f("ix_deployments_os_project_id"), "deployments", ["os_project_id"], unique=False)

    op.add_column(
        "user_openstack_credentials",
        sa.Column("project_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.drop_index("ix_user_openstack_credentials_userId", table_name="user_openstack_credentials")
    op.create_index(
        op.f("ix_user_openstack_credentials_userId"), "user_openstack_credentials", ["userId"], unique=False
    )
    op.create_index(
        op.f("ix_user_openstack_credentials_project_id"), "user_openstack_credentials", ["project_id"], unique=False
    )
    op.create_index(
        "uq_user_openstack_credentials_project",
        "user_openstack_credentials",
        ["userId", "project_id"],
        unique=True,
        postgresql_where=sa.text("project_verified"),
    )

    # Live: not hidden, and no successful destroy (a failed destroy still has
    # servers booted from the images). Runs with the owner's rights, so the
    # worker needs no right on deployments or tasks themselves.
    op.execute(
        """
        CREATE VIEW worker_live_deployments AS
        SELECT d."deploymentId", d."appId", d.os_project_id
          FROM deployments d
         WHERE d.deleted_at IS NULL
           AND NOT EXISTS (
                SELECT 1 FROM tasks t
                 WHERE t."deploymentId" = d."deploymentId"
                   AND t.type = 'DESTROY' AND t.status = 'SUCCESS')
        """
    )
    op.execute(f"GRANT SELECT ON worker_live_deployments TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS worker_live_deployments")
    # Back to one credential per user: keep the newest of each user.
    op.execute(
        """
        DELETE FROM user_openstack_credentials c
         WHERE EXISTS (
                SELECT 1 FROM user_openstack_credentials n
                 WHERE n."userId" = c."userId"
                   AND (n.updated_at, n."credentialId") > (c.updated_at, c."credentialId"))
        """
    )
    op.drop_index("uq_user_openstack_credentials_project", table_name="user_openstack_credentials")
    op.drop_column("user_openstack_credentials", "project_verified")
    op.drop_index(op.f("ix_user_openstack_credentials_project_id"), table_name="user_openstack_credentials")
    op.drop_index(op.f("ix_user_openstack_credentials_userId"), table_name="user_openstack_credentials")
    op.create_index(
        "ix_user_openstack_credentials_userId", "user_openstack_credentials", ["userId"], unique=True
    )
    op.drop_index(op.f("ix_deployments_os_project_id"), table_name="deployments")
    op.drop_column("deployments", "os_project_id")
    op.drop_column("tasks", "state_token_hash")
    op.drop_table("terraform_states")
