import asyncio
import logging
import os
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import SessionLocal
from app.routers import (
    admin_apps,
    apps,
    auth_keycloak,
    courses,
    dashboard,
    deployments,
    internal_tfstate,
    lti13,
    openstack_credentials,
    openstack_resources,
    quotas,
    tasks,
    teams,
    users,
)
from app.services import task_service
from app.services.reconciler import run_reconciler
from app.services.task_events import hub
from app.utils.cross_site import CrossSiteWriteGuard

logger = logging.getLogger(__name__)


# ``DISABLE_BACKGROUND_TASKS`` short-circuits the lifespan body so the
# app is fully wired but the event hub and the reconciler are not
# started. Used by the test suite, where per-TestClient lifespans would
# otherwise stack background loops and exhaust the DB connection pool.
def _background_tasks_disabled() -> bool:
    return os.getenv("DISABLE_BACKGROUND_TASKS", "").lower() in ("1", "true", "yes")


# ----------------------------------------------------------------
# STARTUP/SHUTDOWN
# ----------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("=== Application Starting ===")
    logger.info("ℹ️  Use 'alembic upgrade head' to apply database migrations")

    if _background_tasks_disabled():
        # Test path: keep ``app`` fully functional but skip the event hub
        # and the reconciler.
        logger.info(
            "DISABLE_BACKGROUND_TASKS set — skipping event hub and reconciler (test mode)"
        )
        try:
            yield
        finally:
            logger.info("=== Application Shutting Down (test mode) ===")
        return

    # One LISTEN connection per process (.github#5): wakes the live
    # streams of this process, and the reconciler when a task ends.
    reconcile_now = asyncio.Event()

    releases: set[asyncio.Task] = set()

    def _released(task_id: str) -> None:
        # A worker let go of a task: a destroy parked behind it may run.
        task = asyncio.get_running_loop().create_task(asyncio.to_thread(_release_parked, task_id))
        releases.add(task)
        task.add_done_callback(_release_done(releases))

    hub.on_terminal = lambda _task_id: reconcile_now.set()
    hub.on_released = _released
    hub.start()
    logger.info("Task event hub started")

    # Reconciler: dead workers, lost dispatches, parked messages and the
    # exactly-once follow-up of finished tasks (task_finalizer). Runs as
    # an asyncio task so we can cancel it cleanly on shutdown.
    reconciler_task = asyncio.create_task(run_reconciler(reconcile_now))
    logger.info("Reconciler loop scheduled")

    logger.info("Application started")

    try:
        yield
    finally:
        # Shutdown
        logger.info("=== Application Shutting Down ===")
        reconciler_task.cancel()
        try:
            await reconciler_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Reconciler task raised on shutdown")
        await hub.stop()
        logger.info("Shutdown complete")


def _release_parked(task_id: str) -> None:
    """Release the messages parked behind ``task_id`` (runs in a thread)."""
    with SessionLocal() as db:
        task_service.release_parked(db, UUID(task_id))


def _release_done(releases: set):
    def done(task: asyncio.Task) -> None:
        releases.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Releasing parked tasks failed", exc_info=task.exception())

    return done


def docs_urls(cfg) -> dict:
    """Where the interactive docs and the schema are served: only in development.

    In production they would list every route and parameter to anyone
    (security review M-5). The frontend's types come from the exported
    schema (``scripts/export_openapi.py``), not from the running service.
    """
    if cfg.is_development:
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


# ----------------------------------------------------------------
# FASTAPI APP
# ----------------------------------------------------------------
app = FastAPI(
    title="Backend API",
    description="FastAPI Backend with Auth, Git & Celery Integration",
    version="1.0.0",
    lifespan=lifespan,
    **docs_urls(settings),
)

# ----------------------------------------------------------------
# CORS
# ----------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------
# CROSS-SITE WRITES
# ----------------------------------------------------------------
# Student apps share the platform's site; a write a browser sends for
# another page is refused (review S3). Outermost, so it runs first.
app.add_middleware(CrossSiteWriteGuard, trusted_origins=settings.trusted_write_origins)

# ----------------------------------------------------------------
# ROUTERS
# ----------------------------------------------------------------
app.include_router(auth_keycloak.router, prefix="/auth", tags=["Authentication"])
app.include_router(users.router, prefix="/users", tags=["Users"])
app.include_router(courses.router, prefix="/courses", tags=["Courses"])
app.include_router(apps.router, prefix="/apps", tags=["Apps"])
app.include_router(admin_apps.router, prefix="/admin", tags=["Admin"])
app.include_router(deployments.router, prefix="/deployments", tags=["Deployments"])
app.include_router(tasks.router, prefix="/tasks", tags=["Tasks"])
app.include_router(teams.router, prefix="/teams", tags=["Teams"])
app.include_router(dashboard.router, prefix="/dashboard", tags=["Dashboard"])
app.include_router(lti13.router)
app.include_router(openstack_credentials.router, tags=["OpenStack Credentials"])
app.include_router(quotas.router, tags=["OpenStack Credentials"])
# Read API for the OpenStack resources of one of the caller's credentials
# (networks, flavors, images, ...), used by the wizard's value-help
# dropdowns so users don't have to type UUIDs from Horizon.
app.include_router(
    openstack_resources.router,
    prefix="/me/openstack-credentials/{credential_id}/resources",
    tags=["OpenStack Resources"],
)
# OpenTofu's HTTP state backend for the worker's VM jobs. Not in the
# OpenAPI document; the ingress does not route /api/internal/.
app.include_router(internal_tfstate.router, prefix="/internal/tfstate")


# ----------------------------------------------------------------
# HEALTH CHECK
# ----------------------------------------------------------------
@app.get("/health")
def health_check():
    return {"status": "healthy", "service": "backend-api", "version": "1.0.0"}
