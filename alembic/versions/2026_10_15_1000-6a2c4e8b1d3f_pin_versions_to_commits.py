"""pin approvals and deployments to the commit

Revision ID: 6a2c4e8b1d3f
Revises: 5e1f0c2a9b7d
Create Date: 2026-10-15 10:00:00.000000

An approval covers the commit a tag pointed to when the version was
submitted, and a deployment runs exactly the commit it was created with
(.github#7 G). A tag can be moved after the review; the approval then no
longer applies and the worker never clones by tag name again.

Existing approvals get no commit here (a migration does not reach the Git
hosts). Until ``python -m app.backfill_approval_commits`` has recorded the
commit their tags point to now, or the versions were submitted and approved
again, they do not count as approved: nothing deploys code that was not
pinned at review time.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6a2c4e8b1d3f"
down_revision: Union[str, None] = "5e1f0c2a9b7d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("app_version_approvals", sa.Column("commit_sha", sa.String(length=40), nullable=True))
    op.add_column("deployments", sa.Column("commit_sha", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "commit_sha")
    op.drop_column("app_version_approvals", "commit_sha")
