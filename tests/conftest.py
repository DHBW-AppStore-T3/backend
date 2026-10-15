"""Test-suite-wide fixtures and DB-isolation primitives.

# DB-Isolation: schema once per session, TRUNCATE per test

History — bis Juni 2026 baute jeder Test sein eigenes Schema auf und
riss es danach wieder ab (``Base.metadata.create_all`` /
``drop_all`` als ``autouse=True``-Fixture). Bei einer Suite von ~420
Tests sind das ~840 DDL-Wellen pro Lauf, jede mit FK-Lock-Kaskade
über ein Dutzend Tabellen. Das hat in der Praxis zwei Probleme
erzeugt:

1. **Hänger** — eine offene Test-Session, die noch eine Row in
   ``users`` hielt, blockierte das ``DROP TABLE users`` des nächsten
   Tests; psycopg2 wartete dann ewig im ``recv()`` und der Run blieb
   einfach stehen. Symptom: pytest-Prozess in ``do_sys_poll`` /
   ``futex_wait_queue`` ohne CPU-Last, kein Fortschritt mehr.
2. **Speicher** — Postgres reservierte für jede DDL-Welle frischen
   Backend-Speicher; bei 800+ Wellen sammelten sich genug Verbindungen
   und Catalog-Caches, dass der Container am Ende OOM-gekillt wurde
   (``Error 137`` SIGKILL auf dem nächstbesten Test, hier
   ``test_admin_can_deactivate_app_hides_from_students``, der vier
   Fixtures gleichzeitig zieht und so der nächste anstehende Lock-
   Wait war).

Lösung: Schema einmal pro Test-Session anlegen, zwischen den Tests
nur per ``TRUNCATE ... RESTART IDENTITY CASCADE`` reinigen. Postgres
macht das in einer einzigen Cursor-Operation, kein DDL-Lock, ~50× so
schnell wie der alte Pfad. Identische Test-Semantik (jeder Test
startet mit leeren Tabellen), aber ohne Lock-Stau und ohne
Speicherleck.

# TEST_DATABASE_URL-Gate

Wenn ``TEST_DATABASE_URL`` nicht gesetzt ist, fallen wir auf
``settings.DATABASE_URL`` zurück und warnen einmalig — local-dev mit
einer einzigen Postgres-Instanz darf das (der Entwickler akzeptiert,
dass der Dev-Datenbestand truncate-d wird). CI MUSS ``TEST_DATABASE_URL``
auf den isolierten ``postgres-test``-Service zeigen lassen, sonst
killt die Suite die Dev-Daten.
"""

import os
import uuid
import warnings

# IMPORTANT: this env-var must be set BEFORE ``app.main`` is imported.
# ``lifespan`` reads it on every ``TestClient(app)`` enter to decide
# whether to spawn the Celery event-listener thread and reconciler
# task. Without this gate, each test-client context stacks up another
# listener thread (daemon, never cleanly stopped) that holds DB +
# broker connections — within a handful of tests the SQLAlchemy pool
# is exhausted and the next legit query deadlocks. See ``app/main.py``
# for the full rationale.
os.environ.setdefault("DISABLE_BACKGROUND_TASKS", "1")
# The broker is a table of the application database (pgq, .github#5);
# tests that send for real must write into the test database.
if os.getenv("TEST_DATABASE_URL"):
    os.environ.setdefault("CELERY_BROKER_URL", "pgq+" + os.environ["TEST_DATABASE_URL"])
# App repositories may only come from these hosts (.github#7 B); the tests'
# fixtures use example.com and github.com.
os.environ.setdefault("APP_GIT_ALLOWED_HOSTS", "github.com,example.com")
# The suite runs with the published CI key and plain-HTTP URLs, which only
# development accepts (.github#7 O); tests of the production checks build
# their own Settings.
os.environ.setdefault("APP_ENV", "development")

import hashlib
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import (  # noqa: F401  (importiert für seitliche Effekte / Metadata-Registrierung)
    App,
    User,
    UserRole,
)
from app.utils.keycloak_auth import get_current_user_keycloak

# ----------------------------------------------------------------
# Engine — einmal pro Test-Prozess.
#
# Connection-Pool großzügig dimensioniert, weil mehrere Fixtures
# gleichzeitig Sessions ziehen können (z.B. ``admin_client``,
# ``student_client``, ``db``, ``mock_user``, ``mock_admin`` in einem
# Test) und der dahinterliegende ``TestClient`` zusätzlich
# Dependency-overrides für ``get_db`` öffnet. Bei 7 max-Connections
# (alter Wert) trat in der Praxis Pool-Exhaustion auf, sobald die
# Background-Task-Threads (jetzt deaktiviert) noch dazukamen.
# 20 reicht mit Reserve; sequentielle Suite, Postgres-Container hat
# ``max_connections=100`` per Default.
# ----------------------------------------------------------------
_TEST_DB_URL = os.getenv("TEST_DATABASE_URL", settings.DATABASE_URL)
if _TEST_DB_URL == settings.DATABASE_URL:
    warnings.warn(
        "TEST_DATABASE_URL not set — tests will run against settings.DATABASE_URL "
        "and will TRUNCATE on tear-down. Set TEST_DATABASE_URL to a dedicated test DB.",
        UserWarning,
        stacklevel=2,
    )

engine = create_engine(
    _TEST_DB_URL,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
    pool_recycle=300,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ----------------------------------------------------------------
# Schema-Lifecycle — einmal pro pytest-Session.
# ----------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _setup_schema():
    """Create the full schema once at session start, drop at the end.

    The drop is a safety net for shared Postgres instances; on the
    isolated ``postgres-test`` service it's effectively a no-op
    because the container will be torn down anyway. Using
    ``scope='session'`` means the fixture body runs exactly twice
    (setup + teardown), not 2 × 420 = 840 times.
    """
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _truncate_tables(_setup_schema):
    """Reset DB state to empty between tests.

    Runs AFTER each test (yield first) so a failing test still leaves
    behind useful data for ``pdb`` / log inspection within the test
    itself. The ``TRUNCATE ... RESTART IDENTITY CASCADE`` form is
    Postgres-specific but the whole project is Postgres-only, so this
    is fine.

    Tables are listed in ``sorted_tables`` order (parents first) and
    we pass them in reverse so CASCADE handles the FK chain top-down
    without ``DETAIL`` warnings.

    The fixture depends on ``_setup_schema`` so pytest enforces
    ordering: schema MUST exist before the first truncate runs.
    """
    yield
    table_names = [t.name for t in reversed(Base.metadata.sorted_tables)]
    if not table_names:
        return
    quoted = ", ".join(f'"{n}"' for n in table_names)
    with engine.connect() as conn:
        conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
        conn.commit()


# ----------------------------------------------------------------
# Fake Git remote — no test reaches a real Git host.
# ----------------------------------------------------------------
class FakeGitRemote:
    """Stands in for the app repositories behind ``git_service``.

    ``tags`` maps tag -> commit SHA. With ``any_tag`` (the default) every
    tag a test asks for exists and points to ``sha_for(tag)``; set
    ``any_tag = False`` to make only ``tags`` exist. ``files`` maps a path
    inside the repository to its content and is written into every sparse
    clone, so ``load_variable_definitions`` parses it. ``moved`` makes a
    tag point to another commit from now on (a moved tag).
    """

    def __init__(self) -> None:
        self.any_tag = True
        self.tags: dict[str, str] = {}
        self.files: dict[str, str] = {}
        self.clones: list[tuple[str, str, str]] = []
        self.readable = True

    @staticmethod
    def sha_for(tag: str, salt: str = "") -> str:
        return hashlib.sha1(f"{salt}{tag}".encode()).hexdigest()

    def move(self, tag: str) -> str:
        self.tags[tag] = self.sha_for(tag, salt="moved:")
        return self.tags[tag]

    def ls_remote(self, git_url: str):
        import git as gitpython

        from app.services.git_service import checked_url

        checked_url(git_url)
        if not self.readable:
            raise gitpython.GitCommandError(["git", "ls-remote"], 128, stderr="repository not found")
        tags = dict(self.tags)
        return [{"version": t, "commit": s[:8], "sha": s, "type": "tag"} for t, s in tags.items()]

    def verify(self, git_url: str) -> dict:
        self.ls_remote(git_url)
        return {"success": True, "message": "ok"}

    def resolve(self, git_url: str, tag: str):
        self.ls_remote(git_url)
        if tag in self.tags:
            return self.tags[tag]
        return self.sha_for(tag) if self.any_tag else None

    def clone(self, git_url: str, tag: str, commit_sha: str) -> str:
        self.ls_remote(git_url)
        self.clones.append((git_url, tag, commit_sha))
        path = Path(tempfile.mkdtemp(prefix="fake_vars_"))
        for rel, content in self.files.items():
            target = path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return str(path)


@pytest.fixture(autouse=True)
def fake_git(request, monkeypatch):
    """Every test talks to a ``FakeGitRemote`` unless marked ``real_git``."""
    from app.routers import apps as apps_router
    from app.services import git_service as git_module

    apps_router.clear_variable_cache()
    remote = FakeGitRemote()
    if request.node.get_closest_marker("real_git"):
        yield remote
        return
    svc = git_module.git_service
    monkeypatch.setattr(svc, "_ls_remote_tags", remote.ls_remote)
    monkeypatch.setattr(svc, "resolve_tag", remote.resolve)
    monkeypatch.setattr(svc, "clone_release_vars", remote.clone)
    monkeypatch.setattr(svc, "verify_repository_access", remote.verify)
    yield remote
    apps_router.clear_variable_cache()


# ----------------------------------------------------------------
# DB-Session-Fixture — frische Session pro Test, immer geschlossen.
# ----------------------------------------------------------------
@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        # Defensiv: alle laufenden Transaktionen rollen wir zurück,
        # bevor wir schließen — sonst hält ein open ``begin()`` eine
        # Connection im Pool, die der nächste ``_truncate_tables``
        # auf der gleichen Connection als Lock-Halter sieht.
        try:
            session.rollback()
        finally:
            session.close()


# ----------------------------------------------------------------
# Mock-User-Fixtures
# ----------------------------------------------------------------
@pytest.fixture
def mock_user(db):
    user = User(
        userId=uuid.uuid4(),
        keycloak_id="test-keycloak-id",
        email="test@dhbw.de",
        username="testuser",
        firstName="Test",
        lastName="User",
        role=UserRole.TEACHER,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def mock_admin(db):
    user = User(
        userId=uuid.uuid4(),
        keycloak_id="admin-keycloak-id",
        email="admin@dhbw.de",
        username="adminuser",
        firstName="Admin",
        lastName="User",
        role=UserRole.ADMIN,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def mock_student(db):
    user = User(
        userId=uuid.uuid4(),
        keycloak_id="student-keycloak-id",
        email="student@dhbw.de",
        username="studentuser",
        firstName="Student",
        lastName="User",
        role=UserRole.STUDENT,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ----------------------------------------------------------------
# FastAPI-Test-Clients
# ----------------------------------------------------------------
def _make_client(user):
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            try:
                session.rollback()
            finally:
                session.close()

    def override_get_current_user():
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_keycloak] = override_get_current_user
    return TestClient(app)


@pytest.fixture
def client(mock_user):
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            try:
                session.rollback()
            finally:
                session.close()

    def override_get_current_user():
        return mock_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user_keycloak] = override_get_current_user

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture
def admin_client(mock_admin):
    c = _make_client(mock_admin)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture
def student_client(mock_student):
    c = _make_client(mock_student)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client():
    def override_get_db():
        session = TestingSessionLocal()
        try:
            yield session
        finally:
            try:
                session.rollback()
            finally:
                session.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


# ----------------------------------------------------------------
# SHARED DB HELPERS
# ----------------------------------------------------------------
def approve_version_in_db(db, app, version_tag="v1.0.0", commit_sha=None, status=None):
    """Store an approval of ``version_tag`` at ``commit_sha`` (default: what the fake remote resolves)."""
    from app.models import AppVersionApproval, AppVersionApprovalStatus

    approval = AppVersionApproval(
        appId=app.appId,
        version_tag=version_tag,
        commit_sha=commit_sha or FakeGitRemote.sha_for(version_tag),
        status=status or AppVersionApprovalStatus.APPROVED,
    )
    db.add(approval)
    db.commit()
    db.refresh(approval)
    return approval


def create_app_in_db(db, user, *, name="Test App", git_link="https://github.com/example/repo", is_private=False):
    db_app = App(
        appId=uuid.uuid4(),
        name=name,
        git_link=git_link,
        is_private=is_private,
        userId=user.userId,
    )
    db.add(db_app)
    db.commit()
    db.refresh(db_app)
    return db_app


TEST_PROJECT = "project-1"


def add_credential(
    db,
    user,
    project_id=TEST_PROJECT,
    *,
    project_name="Projekt 1",
    identifier="app-cred-id",
    verified=True,
    auth_url="https://keystone.example/v3",
    credential_id=None,
):
    """A stored application credential of ``user`` for ``project_id``.

    ``verified=False`` is a credential from before projects were taken from
    Keystone (``project_id`` is whatever was typed).
    """
    from app.models import OpenStackAuthType, UserOpenStackCredential
    from app.utils import crypto

    row = UserOpenStackCredential(
        credentialId=credential_id or uuid.uuid4(),
        userId=user.userId,
        auth_type=OpenStackAuthType.APPLICATION_CREDENTIAL,
        auth_url=auth_url,
        region_name="RegionOne",
        project_id=project_id,
        project_name=project_name,
        project_verified=verified,
        encrypted_identifier=crypto.encrypt(identifier),
        encrypted_secret=crypto.encrypt("app-cred-secret"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
