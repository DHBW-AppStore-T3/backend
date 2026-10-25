"""apps.deploy_version: the approved version teachers deploy

Revision ID: e7a9b1d3f5c7
Revises: d6f8a0c2e4b6
Create Date: 2026-10-25 11:00:00.000000

Existing apps get NULL (nothing set).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a9b1d3f5c7"
down_revision: Union[str, None] = "d6f8a0c2e4b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("apps", sa.Column("deploy_version", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("apps", "deploy_version")
