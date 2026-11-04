"""deployments.k8s_scope: one workload per team or per person, as deployed

Revision ID: a2c4e6f8b0d2
Revises: f8b0c2d4e6a8
Create Date: 2026-11-04 12:00:00.000000

Existing deployments get NULL: their scope was not recorded (the version's
appstore.yaml has the default, the job may have carried a choice).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c4e6f8b0d2"
down_revision: Union[str, None] = "f8b0c2d4e6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("k8s_scope", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "k8s_scope")
