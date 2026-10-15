"""LTI launch state in the database, admin deactivation that sticks, narrower queue rights

Revision ID: 9e1a3c5b7d2f
Revises: 8c4e6a0b2d5f
Create Date: 2026-10-15 15:00:00.000000

- ``lti_nonces``: the state/nonce pair of an LTI login in flight. It lived
  in the memory of one API process, so a launch whose two requests reached
  different processes (``uvicorn --workers``) or replicas failed with
  "Unknown or expired state".
- ``apps.hidden_by_admin``: an admin's emergency deactivation; only an admin
  may make such an app public again (.github#7 R).
- ``appstore_worker`` may update only the claim and lease columns of
  ``celery_queue``, no longer a queued message's payload.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e1a3c5b7d2f"
down_revision: Union[str, None] = "8c4e6a0b2d5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WORKER_ROLE = "appstore_worker"
_QUEUE_COLUMNS = "visible_after, claimed_by, delivery_count, after_task"


def upgrade() -> None:
    op.create_table(
        "lti_nonces",
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("state"),
    )
    op.create_index("ix_lti_nonces_created_at", "lti_nonces", ["created_at"])
    op.add_column(
        "apps", sa.Column("hidden_by_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False)
    )
    op.execute(f"REVOKE UPDATE ON celery_queue FROM {WORKER_ROLE}")
    op.execute(f"GRANT UPDATE ({_QUEUE_COLUMNS}) ON celery_queue TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE UPDATE ({_QUEUE_COLUMNS}) ON celery_queue FROM {WORKER_ROLE}")
    op.execute(f"GRANT UPDATE ON celery_queue TO {WORKER_ROLE}")
    op.drop_column("apps", "hidden_by_admin")
    op.drop_index("ix_lti_nonces_created_at", table_name="lti_nonces")
    op.drop_table("lti_nonces")
