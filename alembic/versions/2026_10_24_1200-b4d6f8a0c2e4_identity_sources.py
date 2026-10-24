"""LTI identity by issuer + subject, courses by LTI context, role sources, one-time LTI codes

Revision ID: b4d6f8a0c2e4
Revises: 9e1a3c5b7d2f
Create Date: 2026-10-24 12:00:00.000000

Review S8, S9, S19 and F3 of 2026-10-24:

- ``users.lti_issuer`` / ``users.lti_subject``: the LTI identity an account
  is linked to (it was matched by e-mail on every launch).
- ``courses.lti_issuer`` / ``courses.lti_context_id``: the LTI context of a
  course (it was matched by title).
- ``users.keycloak_role`` / ``lti_role`` / ``granted_role``: what each source
  said last; ``users.role`` is the highest of them (it only ever went up).
  The existing role is kept as it is: an account with a Keycloak id gets it
  as Keycloak's statement (the next request re-reads the token), a teacher as
  the LTI statement (the next launch re-evaluates it), and every admin as a
  grant, so nobody loses access by the migration; review the admins
  (``GET /users/?role=admin``) and demote with ``PUT /users/{id}``.
- ``lti_login_codes``: one-time codes the frontend redeems for a session.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b4d6f8a0c2e4"
down_revision: Union[str, None] = "9e1a3c5b7d2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE = postgresql.ENUM("STUDENT", "TEACHER", "ADMIN", name="userrole", create_type=False)


def upgrade() -> None:
    op.add_column("users", sa.Column("keycloak_role", ROLE, nullable=True))
    op.add_column("users", sa.Column("lti_role", ROLE, nullable=True))
    op.add_column("users", sa.Column("granted_role", ROLE, nullable=True))
    op.add_column("users", sa.Column("lti_issuer", sa.String(), nullable=True))
    op.add_column("users", sa.Column("lti_subject", sa.String(), nullable=True))
    op.create_unique_constraint("uq_users_lti_identity", "users", ["lti_issuer", "lti_subject"])
    op.add_column("courses", sa.Column("lti_issuer", sa.String(), nullable=True))
    op.add_column("courses", sa.Column("lti_context_id", sa.String(), nullable=True))
    op.create_unique_constraint("uq_courses_lti_context", "courses", ["lti_issuer", "lti_context_id"])
    op.create_table(
        "lti_login_codes",
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("binding_hash", sa.String(length=64), nullable=False),
        sa.Column("userId", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["userId"], ["users.userId"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_index("ix_lti_login_codes_created_at", "lti_login_codes", ["created_at"])
    op.create_index(op.f("ix_lti_login_codes_userId"), "lti_login_codes", ["userId"])
    # Nobody's effective role changes here.
    op.execute("UPDATE users SET keycloak_role = role WHERE keycloak_id IS NOT NULL")
    op.execute("UPDATE users SET lti_role = role WHERE role = 'TEACHER'")
    op.execute("UPDATE users SET granted_role = role WHERE role = 'ADMIN'")


def downgrade() -> None:
    op.drop_index(op.f("ix_lti_login_codes_userId"), table_name="lti_login_codes")
    op.drop_index("ix_lti_login_codes_created_at", table_name="lti_login_codes")
    op.drop_table("lti_login_codes")
    op.drop_constraint("uq_courses_lti_context", "courses", type_="unique")
    op.drop_column("courses", "lti_context_id")
    op.drop_column("courses", "lti_issuer")
    op.drop_constraint("uq_users_lti_identity", "users", type_="unique")
    op.drop_column("users", "lti_subject")
    op.drop_column("users", "lti_issuer")
    op.drop_column("users", "granted_role")
    op.drop_column("users", "lti_role")
    op.drop_column("users", "keycloak_role")
