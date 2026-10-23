"""Record the commit of version reviews from before commits were pinned.

Run once after migration ``6a2c4e8b1d3f`` (.github#7 G)::

    python -m app.backfill_approval_commits            # write
    python -m app.backfill_approval_commits --dry-run  # only show

Until then those reviews have no ``commit_sha`` and do not count as
approved, so their versions cannot be deployed and public apps without any
pinned approval disappear from the catalogue for everyone but owners and
admins.

What this does: for every review entry without a commit it records the
commit the tag points to **now**. That accepts today's tag positions as
reviewed. If a tag may have been moved after its review, revoke that
version instead and have it submitted again. Entries whose tag no longer
exists stay unpinned. Repositories outside ``APP_GIT_ALLOWED_HOSTS`` or that
cannot be read anonymously are reported and skipped.
"""

from __future__ import annotations

import argparse
import sys

from app.crud import app_version_approvals as crud_approvals
from app.database import SessionLocal
from app.git_urls import GitHostNotAllowed
from app.models import App, AppVersionApproval
from app.services.git_service import git_service


def _tags(git_link: str) -> dict[str, str]:
    return {v["version"]: v["sha"] for v in git_service.get_versions(git_link)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would be recorded, write nothing")
    args = parser.parse_args(argv)

    failures = 0
    with SessionLocal() as db:
        app_ids = [
            row[0]
            for row in db.query(AppVersionApproval.appId)
            .filter(AppVersionApproval.commit_sha.is_(None))
            .distinct()
            .all()
        ]
        for app_id in app_ids:
            app = db.get(App, app_id)
            if app is None or not app.git_link:
                print(f"{app_id}: no repository, skipped")
                continue
            try:
                tags = _tags(app.git_link)
            except (GitHostNotAllowed, Exception) as e:  # noqa: BLE001 - report and go on
                failures += 1
                print(f"{app.name} ({app.git_link}): cannot read the tags: {e}")
                continue
            if args.dry_run:
                for approval in crud_approvals.get_approvals_for_app(db, app_id):
                    if not approval.commit_sha:
                        sha = tags.get(approval.version_tag)
                        print(f"{app.name} {approval.version_tag} [{approval.status.value}] -> {sha or 'tag gone'}")
                continue
            for tag, sha in crud_approvals.pin_unpinned(db, app_id, tags.get):
                print(f"{app.name} {tag} -> {sha or 'tag gone, left unpinned'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
