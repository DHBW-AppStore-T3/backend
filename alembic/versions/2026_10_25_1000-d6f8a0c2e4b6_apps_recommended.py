"""apps.recommended: admin-curated recommendation for the app catalog

Revision ID: d6f8a0c2e4b6
Revises: b4d6f8a0c2e4
Create Date: 2026-10-25 10:00:00.000000

Existing apps get ``recommended = false`` from the server default.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6f8a0c2e4b6"
down_revision: Union[str, None] = "b4d6f8a0c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "apps",
        sa.Column("recommended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("apps", "recommended")
