import copy
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import git
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.crud import app_version_approvals as crud_approvals
from app.crud import apps as crud_apps
from app.database import get_db
from app.git_urls import GitHostNotAllowed
from app.models import RUNTIME_K8S, RUNTIME_VM, User, UserRole
from app.schemas import (
    AppCreate,
    AppResponse,
    AppUpdate,
    AppVersionApprovalResponse,
    AppVersionApprovalSubmit,
    AppWithVersions,
)
from app.services import app_spec
from app.services.git_service import CommitNotServed, checked_url, git_service
from app.services.hcl_variables import (
    parse_packer_variables,
    parse_terraform_variables,
)
from app.services.packer_discovery import (
    PackerTemplateDiscoveryError,
    discover_packer_templates,
)
from app.utils.app_image import build_image_data_url, parse_image_data_url
from app.utils.capabilities import (
    ensure_delete_app,
    ensure_edit_app,
    ensure_publish_app,
    ensure_register_app,
    ensure_submit_app_version,
    ensure_view_app,
)
from app.utils.keycloak_auth import get_current_user_keycloak

logger = logging.getLogger(__name__)


def _serialize_app(app):
    """Replace ``app.image`` (bytes) with the data-URL form in-place.

    The ORM model carries the raw bytes plus a separate mime column.
    The Pydantic ``AppResponse`` schema declares ``image: Optional[str]``
    and uses ``from_attributes=True``, so Pydantic reads ``app.image``
    directly. Overwriting that attribute with the rebuilt data-URL
    means the response serialiser sees a string and the wire format
    matches the schema. Returns ``app`` so callers can chain.
    """
    if app is None:
        return None
    raw_bytes = getattr(app, "image", None)
    if isinstance(raw_bytes, (bytes, memoryview, bytearray)):
        app.image = build_image_data_url(bytes(raw_bytes), getattr(app, "image_mime", None))
    return app


def _version_tag(version) -> str:
    """Extract the tag string from a git version entry.

    ``git_service.get_versions`` yields dicts carrying the tag under
    ``version``; plain strings are tolerated. Returns ``""`` when nothing
    matches, so callers can treat the result uniformly (empty string is
    falsy and never a valid tag).
    """
    if isinstance(version, str):
        return version
    return version.get("version") or version.get("releaseTag") or version.get("tag", "")


def ensure_allowed_git_link(git_link: str) -> str:
    """The normalised HTTPS URL of ``git_link``; 422 if its host is not allowed.

    App repositories are public and read anonymously from the hosts in
    ``APP_GIT_ALLOWED_HOSTS`` only (.github#7 B).
    """
    try:
        return checked_url(git_link)
    except GitHostNotAllowed as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "git_host_not_allowed", "message": str(e)},
        ) from e


def resolve_version_commit(app, version_tag: str) -> str:
    """The commit ``version_tag`` of ``app`` points to now, as a full SHA.

    What gets approved and deployed is this commit, not the tag name
    (.github#7 G). 400 without a repository, 404 for an unknown tag, 502
    when the repository cannot be read.
    """
    if not app.git_link:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="App has no git repository configured",
        )
    ensure_allowed_git_link(app.git_link)
    if not version_tag:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "version_required", "message": "A version (tag) is required"},
        )
    try:
        sha = git_service.resolve_tag(app.git_link, version_tag)
    except git.GitCommandError as e:
        logger.warning("could not list the tags of %s: %s", app.git_link, (e.stderr or "").strip())
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "git_unreachable", "message": "The app repository cannot be read right now"},
        ) from e
    if sha is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "version_not_found", "message": f"The repository has no tag {version_tag!r}"},
        )
    return sha


router = APIRouter()


# ----------------------------------------------------------------
# Pydantic schema for the /apps/{id}/variables response
# ----------------------------------------------------------------
# Mirrors the exact keys ``_parse_one_variable`` returns (mixed
# snake/camelCase) so the frontend and generated OpenAPI stay in sync.
class _MarkerErrorPayload(BaseModel):
    variable: str
    message: str
    location: str
    code: str | None = None


class AppVariableResponse(BaseModel):
    """Shape of one entry in ``GET /apps/{id}/variables``.

    Keys match what ``_parse_one_variable`` writes to the dict exactly —
    the frontend reads ``osType``/``osMode``/``osMulti``/``osScope``/
    ``varScope``/``fileExtensions`` in camelCase and the rest in
    snake/lowercase. Keys are kept verbatim (no auto-aliasing).
    """

    # ``populate_by_name`` lets callers construct with either field name
    # or alias; the dict-style names are the canonical source.
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    name: str
    type: str
    description: str | None = None
    # Default is typed (Number/Bool/List/Dict/None); ``Any`` is
    # deliberately broad because HCL covers a whole literal family.
    default: Any | None = None
    required: bool
    source: str
    osType: str | None = None
    osMode: str | None = None
    osMulti: bool | None = None
    osScope: str | None = None
    varScope: str | None = None
    fileExtensions: list[str] | None = None
    markerError: _MarkerErrorPayload | None = None
    # ``template_key`` is null for ``source = terraform`` variables and
    # carries the per-template key (``default`` for the legacy layout,
    # or the subdirectory name like ``webserver``/``database`` in
    # multi-image apps) for ``source = packer`` variables. Lets the
    # wizard group Packer variables per image and avoid name collisions
    # across templates.
    template_key: str | None = None


# ----------------------------------------------------------------
# GET ALL APPS
# ----------------------------------------------------------------
@router.get("/", response_model=list[AppResponse])
def list_apps(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """List apps visible to the current user.

    Admins see every non-deleted app (full platform view). Everyone else,
    including teachers, sees the student-style filter: own apps + public
    apps with at least one approved version.
    """
    if current_user.role == UserRole.ADMIN:
        apps = crud_apps.get_apps(db, skip=skip, limit=limit)
    else:
        apps = crud_apps.get_visible_apps(db, current_user.userId, skip=skip, limit=limit)
    return [_serialize_app(a) for a in apps]


# ----------------------------------------------------------------
# GET APP BY ID
# ----------------------------------------------------------------
@router.get("/{app_id}", response_model=AppWithVersions)
def get_app(
    app_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """Get app by ID with available versions.

    Soft-deleted apps are still readable here so existing deployments
    that still reference them can render their app name, git link,
    etc. They just don't show up in the apps list / deploy wizard
    (those use the default-filtered ``get_apps``).

    The owner and admins see every tag. Everyone else sees the approved
    versions whose tag still points to the approved commit; a moved tag
    disappears until it is submitted and approved again.
    """
    app = crud_apps.get_app(db, app_id, include_deleted=True)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Only owner OR admin sees private/unapproved apps; everyone else
    # (incl. teachers) needs the app to be public AND have an approved
    # version. Same gate everywhere via :func:`can_view_app`.
    is_owner_or_admin = (
        str(app.userId) == str(current_user.userId)
        or current_user.role == UserRole.ADMIN
    )
    if not is_owner_or_admin and (app.is_private or not crud_approvals.has_any_approved_version(db, app.appId)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this resource"
            )

    # Fetch versions if git_link exists. Skipped for soft-deleted apps
    # — listing versions is a "what could I deploy" affordance and the
    # answer is "nothing", you already deleted this app.
    if app.git_link and app.deleted_at is None:
        try:
            all_versions = git_service.get_versions(app.git_link)
            if is_owner_or_admin:
                app.versions = all_versions
            else:
                approved = crud_approvals.approved_commits(db, app.appId)
                app.versions = [
                    v for v in all_versions
                    if approved.get(_version_tag(v)) == v.get("sha")
                ]
        except Exception as e:
            app.versions = []
            logger.warning("Could not fetch the versions of app %s: %s", app.appId, e)
    else:
        app.versions = []

    return _serialize_app(app)


# ----------------------------------------------------------------
# GET APP VARIABLES
# ----------------------------------------------------------------
# What a version declares, per (repository, commit): its variables and, for
# a pod app, its validated appstore.yaml. A commit never changes, so an entry
# never goes stale; bounded so a busy catalogue cannot grow it without limit.
# Per process, which is enough: a miss only costs a sparse clone.
@dataclass(frozen=True)
class _VersionScan:
    variables: list[dict[str, Any]]
    spec: app_spec.LoadedSpec | None
    # The 422 detail of an invalid appstore.yaml; nothing of the version is
    # usable then.
    spec_error: dict[str, Any] | None = None


_VARIABLES_CACHE: OrderedDict[tuple[str, str], _VersionScan] = OrderedDict()
_VARIABLES_CACHE_MAX = 256
_variables_cache_lock = threading.Lock()


def clear_variable_cache() -> None:
    """Forget every cached version scan (tests)."""
    with _variables_cache_lock:
        _VARIABLES_CACHE.clear()


def _read_version(repo_path: str) -> _VersionScan:
    """Variables and spec of a cloned commit.

    A pod app (``appstore.yaml`` with ``runtime: kubernetes``) declares its
    variables in the spec; Terraform and Packer files are not looked at.
    Everything else is a VM app.
    """
    try:
        loaded = app_spec.read_spec_from_repo(repo_path)
    except HTTPException as exc:
        return _VersionScan(variables=[], spec=None, spec_error=exc.detail)
    if loaded is not None and loaded.runtime == RUNTIME_K8S:
        return _VersionScan(variables=app_spec.spec_variables(loaded.spec), spec=loaded)

    variables: list[dict[str, Any]] = []
    tf_vars_path = os.path.join(repo_path, "terraform", "variables.tf")
    if os.path.exists(tf_vars_path):
        variables.extend(parse_terraform_variables(tf_vars_path))
    # Discover all Packer templates (legacy single-file layout OR
    # per-key subdirectories) and parse each one's variables. The
    # ``template_key`` is recorded on every Packer variable so the
    # wizard can group inputs per image. Discovery raises if the
    # repo has an ambiguous or unsafe layout — surface that as
    # HTTP 422 so the app author can fix the repo before any
    # deploy attempt.
    try:
        templates = discover_packer_templates(repo_path)
    except PackerTemplateDiscoveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    for tmpl in templates:
        if os.path.isfile(tmpl.variables_path):
            variables.extend(parse_packer_variables(tmpl.variables_path, template_key=tmpl.key))
    return _VersionScan(variables=variables, spec=loaded)


def _scan_version(app, version: str, commit_sha: str | None) -> _VersionScan:
    """What ``version`` declares at ``commit_sha`` (or where the tag points now); cached per commit.

    Fails closed: 404 for an unknown tag, 409 ``version_moved``, 502 when the
    repository cannot be read, 422 for an invalid Packer layout.
    """
    sha = commit_sha or resolve_version_commit(app, version)
    cache_key = (app.git_link or "", sha)
    with _variables_cache_lock:
        cached = _VARIABLES_CACHE.get(cache_key)
        if cached is not None:
            _VARIABLES_CACHE.move_to_end(cache_key)
            return cached

    repo_path = None
    try:
        repo_path = git_service.clone_release_vars(app.git_link, version, sha)
        scan = _read_version(repo_path)
    except HTTPException:
        raise
    except CommitNotServed as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "version_moved", "message": str(e)},
        ) from e
    except (git.GitCommandError, GitHostNotAllowed) as e:
        logger.warning("Could not read app %s at %s: %s", app.appId, sha[:8], e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "git_unreachable", "message": "The app repository cannot be read right now"},
        ) from e
    except Exception:
        logger.exception(
            "Failed to load variable definitions for app %s version %s",
            app.appId, version,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch variables",
        )
    finally:
        if repo_path:
            try:
                git_service.cleanup_repository(repo_path)
            except Exception as cleanup_error:
                logger.error(
                    "Failed to cleanup repository: %s", str(cleanup_error)
                )

    with _variables_cache_lock:
        _VARIABLES_CACHE[cache_key] = scan
        _VARIABLES_CACHE.move_to_end(cache_key)
        while len(_VARIABLES_CACHE) > _VARIABLES_CACHE_MAX:
            _VARIABLES_CACHE.popitem(last=False)
    return scan


def _usable(scan: _VersionScan) -> _VersionScan:
    if scan.spec_error is not None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=scan.spec_error)
    return scan


def load_variable_definitions(app, version: str, commit_sha: str | None = None) -> list[dict[str, Any]]:
    """Parse all variables of one app version into the shape ``GET /apps/{id}/variables`` returns.

    Reads ``commit_sha`` — the commit a submission, approval or deployment
    is about — or, without one, the commit ``version`` points to now: the
    ``appstore.yaml`` variables of a pod app, the Terraform/Packer variables
    of a VM app. Reusable from ``POST /deployments`` so the deployment
    endpoint can enforce per-variable contracts (``varScope``,
    ``fileExtensions``) using the app author's declarations as source of
    truth.

    Fails closed: 404 for an unknown tag, 502 when the repository cannot
    be read, 422 for an invalid Packer layout or appstore.yaml; callers
    never proceed without the declarations.
    """
    return copy.deepcopy(_usable(_scan_version(app, version, commit_sha)).variables)


def load_version_spec(app, version: str, commit_sha: str | None = None) -> app_spec.LoadedSpec | None:
    """The validated ``appstore.yaml`` of the version at ``commit_sha``, or None without one.

    Same errors as :func:`load_variable_definitions` (one clone serves both).
    """
    return _usable(_scan_version(app, version, commit_sha)).spec


def ensure_no_marker_errors(variables: list[dict[str, Any]], action: str) -> None:
    """422 with the list of bad ``@openstack`` markers, if there are any."""
    marker_errors = [v.get("markerError") for v in variables if v.get("markerError")]
    if marker_errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": (
                    f"Version kann nicht {action} werden — fehlerhafte "
                    "@openstack-Marker in den Variablen-Dateien"
                ),
                "marker_errors": marker_errors,
            },
        )


@router.get("/{app_id}/variables", response_model=list[AppVariableResponse])
def get_app_variables(
    app_id: UUID,
    version: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """
    Get dynamic app variables from app's Git repository
    Parses variables.tf file and returns all configurable variables

    Returns:
    - name: Variable name
    - type: Variable type (string, number, bool, list, map, etc.)
    - description: Variable description
    - default: Default value (if any)
    - required: Whether variable is required

    Reads the commit ``version`` points to now; 404 for an unknown tag,
    502 when the repository cannot be read.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Check access permission. ``ensure_view_app`` enforces the matrix:
    # owner OR admin sees private/unapproved, others need the
    # public+approved combination.
    ensure_view_app(current_user, app, db=db)

    variables = load_variable_definitions(app, version)
    if not variables:
        logger.warning("No variables found for app %s version %s", app_id, version)

    # Marker errors travel per-variable in the ``markerError`` field; the
    # endpoint does not 400 on a single bad marker but leaves the frontend
    # to show it inline, keeping the other variables usable.
    bad = [v for v in variables if v.get("markerError")]
    if bad:
        logger.warning(
            "App %s version %s has %d variable(s) with bad @openstack markers: %s",
            app_id, version, len(bad), [v["name"] for v in bad],
        )

    return variables


class AppRuntimeResponse(BaseModel):
    """How one version runs, for the deploy wizard."""

    runtime: str
    # Pods only: one workload per team or per person, internet access, and
    # what members get to log in.
    scope: str | None = None
    egress: str | None = None
    access: list[dict[str, Any]] = []


@router.get("/{app_id}/runtime", response_model=AppRuntimeResponse)
def get_app_runtime(
    app_id: UUID,
    version: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Whether ``version`` runs as pods (``kubernetes``) or VMs (``openstack-vm``).

    The wizard skips the OpenStack steps for pods and takes the group mode
    from ``scope`` (a VM app without an appstore.yaml has none). Same
    visibility and errors as the variables; 422 (with field paths in
    ``errors``) for an invalid appstore.yaml.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")
    ensure_view_app(current_user, app, db=db)
    loaded = load_version_spec(app, version)
    if loaded is None:
        return AppRuntimeResponse(runtime=RUNTIME_VM)
    if loaded.runtime != RUNTIME_K8S:
        # A VM app may declare how members are grouped; ``user`` (the
        # Windows app: one VM per person) makes the wizard put everyone in
        # a team of their own.
        return AppRuntimeResponse(runtime=RUNTIME_VM, scope=loaded.spec.scope)
    return AppRuntimeResponse(
        runtime=RUNTIME_K8S,
        scope=loaded.spec.scope,
        egress=loaded.spec.egress,
        access=[a.model_dump(by_alias=True, exclude_none=True) for a in loaded.spec.access],
    )


# Registering an app with ``submit_all_versions`` reads every submitted
# version (one sparse clone each); the newest ones are enough to start with,
# older tags can still be submitted one by one.
AUTO_SUBMIT_MAX_VERSIONS = 20


def _submit_at(db: Session, app, version_tag: str, commit_sha: str, diff_url=None, notes=None):
    """Submit ``version_tag`` at ``commit_sha`` with the runtime, spec hash and digests the review covers.

    422 for invalid ``@openstack`` markers or an invalid appstore.yaml, 502
    when the repository cannot be read (nothing is submitted then).
    """
    ensure_no_marker_errors(load_variable_definitions(app, version_tag, commit_sha), "eingereicht")
    # The review covers the runtime and, for pods, exactly this spec and
    # these images (.github#4); served from the scan above.
    loaded = load_version_spec(app, version_tag, commit_sha)
    is_pods = loaded is not None and loaded.runtime == RUNTIME_K8S
    return crud_approvals.submit_version(
        db,
        app_id=app.appId,
        version_tag=version_tag,
        commit_sha=commit_sha,
        diff_url=diff_url,
        notes=notes,
        runtime=RUNTIME_K8S if is_pods else RUNTIME_VM,
        spec_sha256=loaded.sha256 if is_pods else None,
        image_digests=app_spec.digests_json(loaded) if is_pods else None,
    )


# ----------------------------------------------------------------
# CREATE APP
# ----------------------------------------------------------------
@router.post("/", response_model=AppResponse, status_code=status.HTTP_201_CREATED)
def create_app(
    app: AppCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """
    Create a new app
    - Teachers and admins only (403 ``role_required``, .github#7 F)
    - The repository must be public and on an allowed host
      (``APP_GIT_ALLOWED_HOSTS``); 422 otherwise, 403 if it cannot be read
    - The stored ``git_link`` is the normalised, credential-free HTTPS URL
    """
    ensure_register_app(current_user)
    # Decode the optional image data-URL up front so a malformed
    # payload fails before we hit Git / DB.
    image_bytes, image_mime = parse_image_data_url(app.image)

    if app.git_link:
        app.git_link = ensure_allowed_git_link(app.git_link)
        access_result = git_service.verify_repository_access(app.git_link)
        if not access_result['success']:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=access_result['message']
            )
        logger.info("Repository access verified for %s", app.git_link)

    db_app = crud_apps.create_app(db, app, current_user.userId)
    if image_bytes is not None:
        db_app = crud_apps.set_app_image(db, db_app.appId, image_bytes, image_mime)

    # Auto-submit the newest tags for review if requested (public apps
    # only), each at the commit it points to now and with what the review
    # covers, exactly as a manual submission would.
    if app.submit_all_versions and not app.is_private and app.git_link:
        try:
            versions = git_service.get_versions(app.git_link)
        except Exception as e:
            versions = []
            logger.warning("Could not auto-submit versions for app %s: %s", db_app.appId, e)
        for v in versions[:AUTO_SUBMIT_MAX_VERSIONS]:
            tag, sha = _version_tag(v), v.get("sha")
            if not tag or not sha:
                continue
            try:
                _submit_at(db, db_app, tag, sha)
            except HTTPException as e:
                db.rollback()
                logger.info("Not auto-submitting %s of app %s: %s", tag, db_app.appId, e.detail)

    return _serialize_app(db_app)


# ----------------------------------------------------------------
# UPDATE APP
# ----------------------------------------------------------------
@router.put("/{app_id}", response_model=AppResponse)
def update_app(
    app_id: UUID,
    app_update: AppUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """Update an app.

    ``git_link`` is immutable after creation — sending it in the body
    returns HTTP 400. Use ``is_private`` to toggle visibility.

    Owner OR admin only. An app an admin deactivated (``hidden_by_admin``)
    can be made public again only by an admin (403 ``app_hidden_by_admin``
    for the owner); an admin doing so lifts the deactivation (.github#7 R).
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Check access permission — owner-or-admin only.
    ensure_edit_app(current_user, app)
    publishing = app_update.is_private is False and app.is_private
    if publishing:
        ensure_publish_app(current_user, app)

    image_was_provided = "image" in app_update.model_fields_set
    image_bytes, image_mime = (None, None)
    if image_was_provided:
        image_bytes, image_mime = parse_image_data_url(app_update.image)

    updated_app = crud_apps.update_app(db, app_id, app_update)
    if publishing and app.hidden_by_admin:
        updated_app = crud_apps.set_hidden_by_admin(db, app_id, False)
    if image_was_provided:
        updated_app = crud_apps.set_app_image(db, app_id, image_bytes, image_mime)
    return _serialize_app(updated_app)


# ----------------------------------------------------------------
# SUBMIT VERSION FOR REVIEW
# ----------------------------------------------------------------
@router.post(
    "/{app_id}/versions/{version_tag}/submit",
    response_model=AppVersionApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_version(
    app_id: UUID,
    version_tag: str,
    body: AppVersionApprovalSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Submit a version tag for admin review, at the commit it points to now.

    Owner OR admin only. A REJECTED version, or one whose tag has moved
    since its review, can be resubmitted; a PENDING or APPROVED one at the
    same commit cannot (409). 422 for invalid ``@openstack`` markers, 404
    for an unknown tag, 502 when the repository cannot be read.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    ensure_submit_app_version(current_user, app)

    commit_sha = resolve_version_commit(app, version_tag)
    return _submit_at(db, app, version_tag, commit_sha, diff_url=body.diff_url, notes=body.notes)


# ----------------------------------------------------------------
# WITHDRAW VERSION SUBMISSION
# ----------------------------------------------------------------
@router.delete(
    "/{app_id}/versions/{version_tag}/submit",
    status_code=status.HTTP_204_NO_CONTENT,
)
def withdraw_version(
    app_id: UUID,
    version_tag: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Withdraw a PENDING version submission.

    Owner OR admin only. Deletes the approval entry so the version
    appears as unsubmitted again.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    ensure_submit_app_version(current_user, app)
    crud_approvals.withdraw(db, app_id=app_id, version_tag=version_tag)
    return None


# ----------------------------------------------------------------
# GET VERSION APPROVALS FOR APP
# ----------------------------------------------------------------
@router.get(
    "/{app_id}/versions",
    response_model=list[AppVersionApprovalResponse],
)
def list_version_approvals(
    app_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """List all version approval entries for an app.

    Owner OR admin only.
    """
    app = crud_apps.get_app(db, app_id, include_deleted=True)
    if not app:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="App not found")

    ensure_edit_app(current_user, app)

    return crud_approvals.get_approvals_for_app(db, app_id)


# ----------------------------------------------------------------
# DELETE APP
# ----------------------------------------------------------------
@router.delete("/{app_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_app(
    app_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """Soft-delete an app.

    Sets ``apps.deleted_at`` instead of removing the row, so any
    historical or still-running deployment that points at this app keeps
    resolving. The app stops appearing in listings and the deploy wizard;
    existing deployments live on until destroyed individually.

    Owner OR admin only.
    """
    app = crud_apps.get_app(db, app_id)
    if not app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )

    # Check access permission — owner-or-admin only.
    ensure_delete_app(current_user, app)

    success = crud_apps.soft_delete_app(db, app_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App not found"
        )
    return None
