"""Management commands: ``python -m app.cli <command>``.

Like ``manage.py`` of a Django project: run by an operator (or a Kubernetes
Job) inside a container that has the database credentials, with the same code
as the API. There is no HTTP endpoint, no login and no override of the API's
authentication; the commands work on a database session through the CRUD and
service layer, with the same validation as the API path (allowed git hosts,
readable public repository, ``appstore.yaml`` / spec hash, marker scan).

``seed`` fills one environment with demo data and is idempotent (nothing is
deleted, nothing that exists is overwritten):

  - the users of the Keycloak realm are provisioned into the database exactly
    like their first login (``sync_user_from_keycloak``, same Keycloak id)
  - the demo course; the realm's teachers teach it, the realm's students
    without a course join it
  - the apps of the config file are registered (public, owned by the first
    admin), their versions submitted and approved
"""

import argparse
import json
import logging
import os
import sys
import urllib.parse
import urllib.request

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.crud import app_version_approvals as crud_approvals
from app.crud import apps as crud_apps
from app.crud import courses as crud_courses
from app.models import App, AppVersionApprovalStatus, Course, CourseTeacher, User, UserRole
from app.schemas import AppCreate, CourseCreate
from app.services.git_service import git_service

log = logging.getLogger("cli.seed")


def _detail(exc: HTTPException) -> str:
    return f"{exc.status_code} {exc.detail}"[:300]


# ----------------------------------------------------------------
# Keycloak realm users
# ----------------------------------------------------------------
def _kc(base: str, path: str, data: bytes | None = None, token: str | None = None):
    req = urllib.request.Request(f"{base}{path}", data=data)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (operator-configured https/http URL)
        return json.load(r)


def realm_users(env=os.environ):
    """The enabled users with an e-mail of the Keycloak realm, with their realm roles.

    Read with the backend's own Keycloak service client (client credentials),
    the same credentials the API already uses for Keycloak.
    """
    base = env["KEYCLOAK_SERVER_URL"].rstrip("/")
    realm = env["KEYCLOAK_REALM"]
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": env["KEYCLOAK_CLIENT_ID"],
            "client_secret": env["KEYCLOAK_CLIENT_SECRET"],
        }
    ).encode()
    token = _kc(base, f"/realms/{realm}/protocol/openid-connect/token", body)["access_token"]
    for u in _kc(base, f"/admin/realms/{realm}/users?max=500", token=token):
        if u.get("username", "").startswith("service-account-") or not u.get("email") or not u.get("enabled", True):
            continue
        roles = _kc(base, f"/admin/realms/{realm}/users/{u['id']}/role-mappings/realm", token=token)
        u["roles"] = [r["name"] for r in roles]
        yield u


# ----------------------------------------------------------------
# seed
# ----------------------------------------------------------------
def seed_users(db: Session, realm: list[dict]) -> list[User]:
    from app.utils.keycloak_auth import sync_user_from_keycloak

    users = []
    for u in realm:
        try:
            users.append(sync_user_from_keycloak(db, u))
        except Exception as exc:  # e.g. the same e-mail under another Keycloak id: keep going
            db.rollback()
            log.warning("user %s not provisioned: %s", u.get("username"), exc)
    log.info("%d realm users provisioned", len(users))
    return users


def seed_course(db: Session, name: str, users: list[User]) -> Course:
    course = db.query(Course).filter(Course.name == name).first()
    if course is None:
        course = crud_courses.create_course(db, CourseCreate(name=name))
        log.info("course %s created", course.name)
    linked = {t.userId for t in db.query(CourseTeacher).filter(CourseTeacher.courseId == course.courseId)}
    for u in users:
        if u.role == UserRole.TEACHER and u.userId not in linked:
            db.add(CourseTeacher(courseId=course.courseId, userId=u.userId))
            log.info("teacher %s -> %s", u.email, course.name)
    db.commit()
    for u in users:
        if u.role == UserRole.STUDENT and u.courseId is None:
            crud_courses.add_users_to_course(db, course.courseId, [u.userId])
            log.info("student %s -> %s", u.email, course.name)
    return course


def seed_apps(db: Session, owner: User, apps: list[dict]) -> None:
    """Register, submit and approve the apps, as the owner/admin ``owner`` would
    through the API but without HTTP: the same checks, the same CRUD."""
    # Imported here: these are the API's own validation helpers.
    from app.routers.admin_apps import approve_submitted_version
    from app.routers.apps import _submit_at, ensure_allowed_git_link, resolve_version_commit

    for a in apps:
        row = db.query(App).filter(App.name == a["name"], App.deleted_at.is_(None)).first()
        if row is None:
            try:
                link = ensure_allowed_git_link(a["gitLink"]) if a.get("gitLink") else None
                if link:
                    access = git_service.verify_repository_access(link)
                    if not access["success"]:
                        raise HTTPException(status_code=403, detail=access["message"])
                row = crud_apps.create_app(
                    db,
                    AppCreate(name=a["name"], description=a.get("description"), git_link=link, is_private=False),
                    owner.userId,
                )
            except HTTPException as exc:
                log.warning("app %s not created: %s", a["name"], _detail(exc))
                continue
            log.info("app %s created (owner %s)", a["name"], owner.email)
        for tag in a.get("versions", []):
            try:
                db.expire_all()
                ap = crud_approvals.get_approval(db, row.appId, tag)
                if ap is not None and ap.status == AppVersionApprovalStatus.APPROVED:
                    continue
                if ap is None or ap.status == AppVersionApprovalStatus.REJECTED:
                    _submit_at(db, row, tag, resolve_version_commit(row, tag), notes="Demo-Seed")
                approve_submitted_version(db, row, tag, owner.userId)
                log.info("%s %s approved", a["name"], tag)
            except HTTPException as exc:
                db.rollback()
                log.warning("%s %s not published: %s", a["name"], tag, _detail(exc))


def seed(db: Session, cfg: dict, realm: list[dict]) -> None:
    users = seed_users(db, realm)
    if cfg.get("courseName") and users:
        seed_course(db, cfg["courseName"], users)
    apps = cfg.get("apps", [])
    owner = db.query(User).filter(User.role == UserRole.ADMIN).order_by(User.created_at).first() if apps else None
    if apps and owner is None:
        log.warning("no admin user: demo apps skipped")
    elif apps:
        seed_apps(db, owner, apps)
    log.info("done")


def _cmd_seed(args) -> None:
    from app.database import SessionLocal

    with open(args.config) as f:
        cfg = json.load(f)
    db = SessionLocal()
    try:
        seed(db, cfg, list(realm_users()))
    finally:
        db.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("seed", help="idempotent demo data: realm users, demo course, catalog apps")
    p.add_argument("--config", default=os.environ.get("SEED_JSON", "/seed/seed.json"), help="JSON with courseName and apps")
    p.set_defaults(func=_cmd_seed)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    try:
        args.func(args)
    except Exception as exc:
        # Non-zero exit: a failing Job is visible in Argo CD and kubectl.
        log.error("failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
