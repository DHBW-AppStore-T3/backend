"""Drop what only the OpenStack VM path used

Revision ID: c5e7a9b1d3f5
Revises: e7a9b1d3f5c7
Create Date: 2026-10-25 13:00:00.000000

Every app runs as pods now (.github#12); the Windows app will run on KubeVirt
and needs none of this:

- ``terraform_states``: the OpenTofu state of VM deployments.
- ``user_openstack_credentials`` and the ``openstackauthtype`` enum.
- ``deployments.os_project_id`` and ``tasks.state_token_hash``.
- the view ``worker_live_deployments`` (app and project of live deployments,
  which only the VM worker's image cleanup read).
- the default ``runtime`` of deployments and approvals is ``kubernetes``
  (was ``openstack-vm``). Existing rows keep the value they have.

Kept on purpose: ``tasks.tf_state`` / ``tasks.outputs`` (history of finished
tasks) and the ``runtime`` columns themselves (a KubeVirt runtime follows).

The downgrade recreates the empty tables and columns, not their data.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c5e7a9b1d3f5"
down_revision: Union[str, None] = "e7a9b1d3f5c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WORKER_ROLE = "appstore_worker"

OPENSTACK_AUTH_TYPE = postgresql.ENUM(
    "v3applicationcredential",
    "password",
    name="openstackauthtype",
    create_type=False,
)


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS worker_live_deployments")
    op.drop_table("terraform_states")
    op.drop_table("user_openstack_credentials")
    OPENSTACK_AUTH_TYPE.drop(op.get_bind(), checkfirst=True)
    op.drop_index(op.f("ix_deployments_os_project_id"), table_name="deployments")
    op.drop_column("deployments", "os_project_id")
    op.drop_column("tasks", "state_token_hash")
    op.alter_column("deployments", "runtime", server_default="kubernetes")
    op.alter_column("app_version_approvals", "runtime", server_default="kubernetes")


def downgrade() -> None:
    op.alter_column("app_version_approvals", "runtime", server_default="openstack-vm")
    op.alter_column("deployments", "runtime", server_default="openstack-vm")
    op.add_column("tasks", sa.Column("state_token_hash", sa.String(length=64), nullable=True))
    op.add_column("deployments", sa.Column("os_project_id", sa.String(), nullable=True))
    op.create_index(op.f("ix_deployments_os_project_id"), "deployments", ["os_project_id"], unique=False)

    OPENSTACK_AUTH_TYPE.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "user_openstack_credentials",
        sa.Column("credentialId", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("userId", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("auth_type", OPENSTACK_AUTH_TYPE, nullable=False),
        sa.Column("auth_url", sa.String(), nullable=False),
        sa.Column("region_name", sa.String(), nullable=True),
        sa.Column("interface", sa.String(), nullable=True),
        sa.Column("identity_api_version", sa.String(), nullable=True),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("project_name", sa.String(), nullable=True),
        sa.Column("user_domain_name", sa.String(), nullable=True),
        sa.Column("project_domain_name", sa.String(), nullable=True),
        sa.Column("project_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("encrypted_identifier", sa.LargeBinary(), nullable=False),
        sa.Column("encrypted_secret", sa.LargeBinary(), nullable=False),
        sa.Column("last_validated_at", sa.DateTime(), nullable=True),
        sa.Column("last_validation_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["userId"], ["users.userId"], ondelete="CASCADE"),
    )
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
