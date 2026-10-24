"""Move the OpenTofu state of existing VM deployments into the API.

Run once after migration ``7b3d5f9a2c4e``, while no VM job runs (stop the
VM workers first)::

    TFSTATE_DATABASE_URL=postgresql://... python -m app.import_pg_tfstate            # write
    TFSTATE_DATABASE_URL=postgresql://... python -m app.import_pg_tfstate --dry-run  # only show

Until then VM jobs of these deployments start from an empty state through
the new HTTP backend and would try to create every resource again; the
resource views show the copy of their last task, if any.

The old jobs kept each deployment's state with Terraform's ``pg`` backend,
in the separate tfstate database: schema ``deployment_<uuid with _>``,
table ``states``, workspace ``default``. Each one found is stored
encrypted in ``terraform_states`` unless the deployment already has a
state there (then it is left alone and reported). The old database is not
changed; drop it once every live deployment has been checked.
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg
from psycopg import sql

from app.database import SessionLocal
from app.models import Deployment
from app.pgq import libpq_url
from app.services import tf_state_store


def schema_name(deployment_id: str) -> str:
    """The worker's old schema name for a deployment (``tasks._tfstate_schema_name``)."""
    return f"deployment_{deployment_id.replace('-', '_')}"


def read_old_state(conn: psycopg.Connection, deployment_id: str) -> str | None:
    """The deployment's state in the old ``pg`` backend, or None if it has none."""
    schema = schema_name(deployment_id)
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = 'states'",
        (schema,),
    ).fetchone()
    if exists is None:
        return None
    row = conn.execute(
        sql.SQL("SELECT data FROM {}.states WHERE name = 'default'").format(sql.Identifier(schema))
    ).fetchone()
    return row[0] if row and row[0] else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would be moved, write nothing")
    parser.add_argument(
        "--tfstate-url",
        default=os.environ.get("TFSTATE_DATABASE_URL", ""),
        help="the old tfstate database (default: $TFSTATE_DATABASE_URL)",
    )
    parser.add_argument(
        "--include-deleted", action="store_true", help="also deleted deployments (normally their state is empty)"
    )
    args = parser.parse_args(argv)
    if not args.tfstate_url:
        parser.error("set TFSTATE_DATABASE_URL or pass --tfstate-url")

    failures = 0
    moved = 0
    with psycopg.connect(libpq_url(args.tfstate_url), autocommit=True) as old, SessionLocal() as db:
        query = db.query(Deployment.deploymentId)
        if not args.include_deleted:
            query = query.filter(Deployment.deleted_at.is_(None))
        for (deployment_id,) in query.all():
            try:
                state = read_old_state(old, str(deployment_id))
            except psycopg.Error as e:
                failures += 1
                print(f"{deployment_id}: cannot read the old state: {e}")
                continue
            if state is None:
                continue
            if args.dry_run:
                print(f"{deployment_id}: {len(state)} bytes would be moved")
                continue
            try:
                stored = tf_state_store.import_state(db, deployment_id, state)
            except ValueError:
                failures += 1
                db.rollback()
                print(f"{deployment_id}: old state is not valid JSON, skipped")
                continue
            if stored:
                moved += 1
                print(f"{deployment_id}: moved")
            else:
                print(f"{deployment_id}: already has a state in the API, left alone")
    print(f"{moved} state(s) moved, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
