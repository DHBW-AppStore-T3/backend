import asyncio
import base64
import binascii
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.crud import app_version_approvals as crud_approvals
from app.crud import apps as crud_apps
from app.crud import deployments as crud_deployments
from app.crud import locks as crud_locks
from app.crud import teams as crud_teams
from app.crud import users as crud_users
from app.database import SessionLocal, get_db
from app.k8s.render import Member, workload_refs
from app.k8s.render import Team as RenderTeam
from app.k8s.spec import MAX_FILE_VARIABLE_BYTES, parse_size_bytes
from app.models import RUNTIME_K8S, TaskStatus, TaskType, User, UserRole
from app.models import Task as TaskModel  # for ad-hoc state queries
from app.schemas import (
    DeploymentCreate,
    DeploymentDetail,
    DeploymentOutputs,
    DeploymentPermissions,
    DeploymentResourceListResponse,
    DeploymentResponse,
    DeploymentTeamMember,
    DeploymentTeamResponse,
    MyAccessResponse,
    PodWorkloadSchema,
    TaskSummary,
)
from app.services import (
    access,
    app_spec,
    k8s_status,
    task_events,
    task_finalizer,
    task_results,
)
from app.services import lifecycle as lifecycle_service
from app.services import task_service as task_service_module
from app.services.deployment_input import (
    attach_files_to_user_input,
    looks_like_file_var,
    parse_and_strip_user_input,
    validate_scoped_user_input,
)
from app.task_contract import (
    EVENT_FAILED,
    EVENT_REVOKED,
    EVENT_SUCCEEDED,
    NOTIFY_TASK_CANCEL,
    TERMINAL_EVENTS,
    terminal_payload,
)
from app.utils import rate_limit
from app.utils.capabilities import (
    can_operate_deployment,
    can_view_deployment_owner,
    ensure_deploy,
    ensure_deploy_app,
    ensure_operate_deployment,
    ensure_view_app,
    ensure_view_deployment,
    ensure_view_deployment_member,
    ensure_view_deployment_owner,
)
from app.utils.filenames import content_disposition
from app.utils.keycloak_auth import get_current_user_keycloak
from app.utils.time import utcnow

logger = logging.getLogger(__name__)
router = APIRouter()


# ----------------------------------------------------------------
# GET ALL DEPLOYMENTS
# ----------------------------------------------------------------
@router.get("/", response_model=list[DeploymentResponse])
def list_deployments(
    skip: int = 0,
    limit: int = 100,
    app_id: UUID | None = None,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """List deployments visible to the caller.

    Visibility rules:
      * **Staff** (teachers and admins): deployments they created —
        their own owned set. Cross-user listing is intentional UX: a
        teacher opens an individual deployment via direct link or via
        a student's profile page, not from this index.
      * **Students** (and any non-staff role): deployments they own
        OR are a member of — either via a team mapping or a direct
        ``UserToDeployment`` row.

    A ``?scope=course`` branch used to sit here, listing every
    deployment whose owner was in a course the caller teaches. It had
    no caller in the frontend and no test, it silently ignored
    ``status_filter``, and its admin arm was a comment block ending in
    ``pass``. Removed rather than repaired: if the
    teacher-inspects-a-student feature comes back it wants its own
    endpoint with a working filter and tests, not this.
    """
    is_staff = current_user.role in (UserRole.TEACHER, UserRole.ADMIN)

    if is_staff:
        deployments = crud_deployments.get_deployments(
            db,
            skip=skip,
            limit=limit,
            user_id=current_user.userId,
            app_id=app_id,
            status=status_filter,
        )
    else:
        deployments = crud_deployments.get_deployments(
            db,
            skip=skip,
            limit=limit,
            member_user_id=current_user.userId,
            app_id=app_id,
            status=status_filter,
        )

    # Enrich with status and created_at from tasks. The summary is
    # bulk-fetched in two queries (latest + first task per deployment via
    # window functions) so the list endpoint stays at a constant query
    # count regardless of page size.
    task_summary = crud_deployments.bulk_get_task_summary(
        db, [d.deploymentId for d in deployments]
    )

    result = []
    for deployment in deployments:
        # Pull the latest-task ``(status, type)`` and the first-task
        # timestamp out of the bulk map. Deployments without any task
        # yet (new row, dispatch in flight) map to ``(None, None, None)``
        # — ``derive_status`` returns None for that, which the schema
        # accepts (``status: str | None``).
        latest_status, latest_type, first_created_at = task_summary.get(
            deployment.deploymentId, (None, None, None)
        )
        status_value = crud_deployments.derive_status(latest_status, latest_type)

        # Parse userInputVar JSON string back to dict if it exists.
        # File uploads are stripped down to metadata here so the list
        # view doesn't ship megabytes of base64 to the browser; the
        # detail endpoint follows the same rule, and the dedicated
        # download route is the only path that returns raw bytes.
        user_input_var_parsed = parse_and_strip_user_input(deployment.userInputVar)

        result.append(DeploymentResponse(
            deploymentId=deployment.deploymentId,
            name=deployment.name,
            appId=deployment.appId,
            userId=deployment.userId,
            releaseTag=deployment.releaseTag,
            commit_sha=deployment.commit_sha,
            runtime=deployment.runtime,
            userInputVar=user_input_var_parsed,
            status=status_value,
            created_at=first_created_at,
        ))

    return result


# ----------------------------------------------------------------
# GET DEPLOYMENT BY ID (Full Details)
# ----------------------------------------------------------------
@router.get("/{deployment_id}", response_model=DeploymentDetail)
def get_deployment(
    deployment_id: UUID,
    include_logs: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """
    Get deployment by ID with full details including:
    - User and App relations
    - Teams with members
    - Latest task status
    - Terraform outputs
    - Optionally: full logs (use include_logs=true)
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found"
        )

    # Member view or owner view (a course-teacher inspecting a student's
    # deployment); 403 otherwise. No blanket teacher access (.github#7 F).
    ensure_view_deployment(current_user, deployment, db)

    # Get latest task
    latest_task = crud_deployments.get_latest_task(db, deployment_id)
    task_summary = None
    logs = None

    if latest_task:
        task_summary = TaskSummary(
            taskId=latest_task.taskId,
            type=latest_task.type,
            status=latest_task.status,
            started_at=latest_task.started_at,
            finished_at=latest_task.finished_at,
            created_at=latest_task.created_at,
            current_phase=getattr(latest_task, "current_phase", None),
            progress_pct=getattr(latest_task, "progress_pct", None),
        )
        if include_logs:
            logs = latest_task.logs

    # Get teams with members. The owner view sees every team and
    # every member. The member view only sees their own team(s) so
    # they can't browse who else has access to the deployment.
    # ``can_view_deployment_owner`` widens "owner view" to course-teachers
    # of the deployment-owner's course, so they get the full roster + logs
    # + outputs alongside owners and admins.
    is_owner_view = can_view_deployment_owner(current_user, deployment, db)
    teams_data = crud_deployments.get_deployment_teams_with_members(db, deployment_id)
    is_member = any(
        str(m["userId"]) == str(current_user.userId) for t in teams_data for m in t["members"]
    )
    if not is_owner_view:
        teams_data = [
            t for t in teams_data
            if any(str(m["userId"]) == str(current_user.userId) for m in t["members"])
        ]
    teams = [
        DeploymentTeamResponse(
            teamId=team["teamId"],
            name=team["name"],
            members=[
                DeploymentTeamMember(
                    userId=member["userId"],
                    email=member["email"],
                    username=member["username"]
                )
                for member in team["members"]
            ]
        )
        for team in teams_data
    ]

    # Outputs / state / logs are owner-only — members don't get to
    # browse the credentials of teammates or the raw infrastructure
    # state. They fetch their own credentials through ``my-access``.
    if is_owner_view:
        # Sensitive outputs (and every member's account) are blanked out;
        # each member fetches their own through ``my-access`` (.github#7 E).
        outputs_data = task_results.redact_sensitive_outputs(
            crud_deployments.get_deployment_outputs(db, deployment_id)
        )
        outputs = DeploymentOutputs(raw=outputs_data) if outputs_data else None
    else:
        outputs = None
        logs = None

    # Get status and created_at from tasks
    status_value = crud_deployments.get_deployment_status(db, deployment_id)
    created_at = crud_deployments.get_deployment_created_at(db, deployment_id)

    # Parse userInputVar JSON string back to dict if it exists. Same
    # strip-file-bytes treatment as the list endpoint — base64
    # payloads are surfaced via the download route, not the JSON view.
    user_input_var_parsed = parse_and_strip_user_input(deployment.userInputVar)

    # ``deployment.app`` is the raw ORM relation whose ``image`` column
    # carries bytes. Pydantic's ``DeploymentDetail`` declares
    # ``app.image: Optional[str]`` (the wire shape is a ``data:image/...``
    # URL), so handing it the bytes verbatim throws ``string_unicode``.
    # Run it through ``_serialize_app`` — the same helper the
    # ``/apps``-endpoints already use — to swap the bytes for the
    # data-URL string in place. Apps without an uploaded image are
    # unaffected (``getattr`` returns ``None`` and the helper no-ops).
    from app.routers.apps import _serialize_app  # local: avoid import cycle
    serialised_app = _serialize_app(deployment.app)

    # The UI renders buttons from these instead of guessing from the role.
    may_operate = can_operate_deployment(current_user, deployment, db)
    permissions = DeploymentPermissions(owner_view=is_owner_view, operate=may_operate, member=is_member)
    actions = (
        sorted(a.value for a in lifecycle_service.allowed_actions(db, deployment)) if may_operate else []
    )

    return DeploymentDetail(
        deploymentId=deployment.deploymentId,
        name=deployment.name,
        appId=deployment.appId,
        userId=deployment.userId,
        releaseTag=deployment.releaseTag,
        commit_sha=deployment.commit_sha,
        runtime=deployment.runtime,
        userInputVar=user_input_var_parsed,
        status=status_value,
        created_at=created_at,
        user=deployment.user,
        app=serialised_app,
        teams=teams,
        latest_task=task_summary,
        outputs=outputs,
        logs=logs,
        permissions=permissions,
        actions=actions,
    )


# ----------------------------------------------------------------
# CREATE DEPLOYMENT
# ----------------------------------------------------------------
@router.post(
    "/",
    response_model=DeploymentResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit.DEPLOY)],
)
def create_deployment(
    deployment: DeploymentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak)
):
    """
    Create a new deployment

    Atomicity: a per-user advisory lock serializes concurrent creates of one
    user (the workload budget). The deployment row, teams, user mappings,
    and the initial PENDING task row are all inserted in a single
    transaction, so the user can never end up with a deployment row
    that has no matching task. The queue dispatch happens AFTER commit;
    if it fails, the task row is flipped to FAILED so the deployment
    surfaces an honest error instead of hanging in PENDING forever.
    """
    # Teachers and admins deploy; students use deployments (.github#7 F).
    ensure_deploy(current_user)
    # Teams are lists of existing users, each in at most one team.
    _validate_teams(db, deployment.teams or [])

    # Per-user lock — serializes concurrent POST /deployments from this user
    # (the workload budget). Held until the next COMMIT/ROLLBACK on this connection.
    crud_locks.acquire_user_xact_lock(db, current_user.userId)

    # Refuse the create if the target app is soft-deleted.
    target_app = crud_apps.get_app(db, deployment.appId)
    if target_app is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "app_not_found_or_deleted"},
        )

    # Gate the create on the same visibility rule the list/detail
    # endpoints use. A student cannot deploy a private app they don't
    # own, and a non-owner cannot deploy an app without an approved
    # version. The owner / admin path stays open.
    # ``ensure_view_app`` raises 403 with the structured payload, so
    # the frontend receives the same shape it sees on the detail
    # endpoint when visibility is denied.
    ensure_view_app(current_user, target_app, db=db)
    # An app an admin deactivated is private, so it would skip the approval
    # gate below: refuse it outright (403 ``app_deactivated``), admins aside.
    ensure_deploy_app(current_user, target_app)

    # The deployment runs the commit the tag points to now, and every job
    # of it runs exactly that commit (.github#7 G). A public app needs an
    # approval of exactly this commit; a private app is its owner's test
    # bench (only the owner and admins get past ``ensure_view_app``).
    # Unknown tag 404, unreadable repository 502.
    from app.routers.apps import (
        load_variable_definitions,
        load_version_spec,
        resolve_version_commit,
    )
    commit_sha = resolve_version_commit(target_app, deployment.releaseTag)
    if not target_app.is_private and not crud_approvals.has_approved_version(
        db, target_app.appId, deployment.releaseTag, commit_sha
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "version_not_approved", "version": deployment.releaseTag},
        )

    # The app's appstore.yaml at the pinned commit (422 without a valid one);
    # the review of a public app covers exactly this spec (.github#4).
    loaded_spec = load_version_spec(target_app, deployment.releaseTag, commit_sha)
    approval = crud_approvals.get_approval(db, target_app.appId, deployment.releaseTag)
    if not target_app.is_private and (
        approval is None or approval.runtime != RUNTIME_K8S or approval.spec_sha256 != loaded_spec.sha256
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "version_not_approved", "version": deployment.releaseTag},
        )

    # The app author's variable declarations at that commit, to enforce
    # per-variable contracts (``varScope``, ``fileExtensions``) below.
    # Fails closed: without the declarations nothing is deployed. Same
    # parser as ``GET /apps/{id}/variables`` so client and server agree on
    # which variable is scoped/file/free-text; cached per commit.
    variable_definitions = load_variable_definitions(target_app, deployment.releaseTag, commit_sha)

    # Fold the wizard's parallel ``files`` upload into
    # ``userInputVar.terraform`` before the row gets persisted, so the
    # rest of this handler — and the worker downstream — sees one
    # uniform dict. The helper validates base64 / size / per-file and
    # per-deployment caps; any failure short-circuits with a 4xx and
    # the row never enters the DB. When variable definitions are
    # available, the helper also enforces the app author's declared
    # ``fileExtensions`` filter on each upload name.
    deployment.userInputVar = attach_files_to_user_input(
        deployment.userInputVar, deployment.files, variable_definitions or None,
    )

    # Enforce ``varScope = team|user`` contracts: each value must be a
    # map whose keys match the deployment's team / user roster. This
    # is defense-in-depth — the wizard already only renders slots the
    # user can fill, but a hand-crafted POST could ship unknown keys.
    if variable_definitions:
        validate_scoped_user_input(
            deployment.userInputVar,
            variable_definitions,
            deployment.teams or [],
        )

    _check_pod_files(loaded_spec, deployment.userInputVar)
    # What the person chose for cpu/memory (only where the app allows it).
    loaded_spec = app_spec.with_resource_overrides(loaded_spec, deployment.userInputVar.get("resources"))
    loaded_spec = app_spec.with_network_overrides(loaded_spec, deployment.userInputVar.get("network"))
    workloads = _check_pod_workloads(db, current_user, loaded_spec, deployment.teams or [])

    db_deployment = crud_deployments.create_deployment(
        db,
        deployment,
        current_user.userId,
        commit_sha,
        runtime=RUNTIME_K8S,
        k8s_workloads=workloads,
    )

    user_ids_in_deployment = set()
    if deployment.teams:
        teams_data = [
            {"name": team.name, "userIds": team.userIds}
            for team in deployment.teams
        ]
        crud_teams.create_teams_for_deployment(
            db=db,
            deployment_id=db_deployment.deploymentId,
            teams_data=teams_data,
        )
        for team in deployment.teams:
            user_ids_in_deployment.update(team.userIds)

    if user_ids_in_deployment:
        crud_deployments.create_user_to_deployments(
            db=db,
            deployment_id=db_deployment.deploymentId,
            user_ids=user_ids_in_deployment,
        )

    # Parse user input variables
    try:
        user_vars = (
            json.loads(db_deployment.userInputVar) if db_deployment.userInputVar else {}
        )
    except Exception:
        user_vars = {}

    # The roster the worker gets: per team the members' e-mail (what the
    # apps' ``users`` variable carries), user id and username. The id lets
    # access data be matched to exactly one person (.github#7 H).
    teams_dict = {}
    if deployment.teams:
        for team in deployment.teams:
            team_users = []
            for user_id in team.userIds:
                user = crud_users.get_user(db, user_id)
                if user:
                    team_users.append(_member_entry(user))
            teams_dict[team.name] = team_users

    # The job carries the validated spec.
    extra = {"spec": loaded_spec.payload(), "owner": str(current_user.userId)}

    # Insert PENDING task row in the SAME transaction as the deployment,
    # commit atomically (deployment + teams + user_to_deployments + task),
    # then dispatch to Celery outside the locked TX. On a send failure the
    # task row is flipped to FAILED and we surface 503 — the deployment
    # row stays, but the user sees an obvious failure instead of an
    # eternal PENDING.
    _commit_and_dispatch(
        db,
        deployment_id=db_deployment.deploymentId,
        task_type=TaskType.DEPLOY,
        celery_task_name=_task_name(TaskType.DEPLOY),
        celery_kwargs=_job_kwargs(db_deployment, user_vars, teams_dict, **extra),
        dispatch_error_label="Could not dispatch deployment task — please retry",
        rollback_on_conflict=True,
    )

    db.refresh(db_deployment)

    status_value = crud_deployments.get_deployment_status(db, db_deployment.deploymentId)
    created_at = crud_deployments.get_deployment_created_at(db, db_deployment.deploymentId)

    # Same file-strip rule as the list/detail endpoints: the POST
    # response shape mirrors the read shape so the frontend can reuse
    # the same parsing code-path.
    user_input_var_parsed = parse_and_strip_user_input(db_deployment.userInputVar)

    return DeploymentResponse(
        deploymentId=db_deployment.deploymentId,
        name=db_deployment.name,
        appId=db_deployment.appId,
        userId=db_deployment.userId,
        releaseTag=db_deployment.releaseTag,
        commit_sha=db_deployment.commit_sha,
        runtime=db_deployment.runtime,
        userInputVar=user_input_var_parsed,
        status=status_value,
        created_at=created_at,
    )


# ----------------------------------------------------------------
# DELETE DEPLOYMENT
# ----------------------------------------------------------------
#
# One endpoint, two outcomes — the backend picks the right one from
# the deployment's status:
#
#   * pod deployment            → dispatch a Destroy task (removes the
#     namespace, auto-soft-delete on success). Returns 202 + task_id; the
#     frontend keeps the live stream open and routes back to the list when
#     the task finishes.
#   * any other runtime (a row left from the OpenStack VM path)
#                               → soft-delete immediately. Returns 204.
#   * an active task (running / pending / destroying / pausing / resuming)
#                               → 409, the user has to wait.
#
# Frontend doesn't have to know the difference — it just calls DELETE
# and switches into the live-stream view when the response is 202.
@router.delete("/{deployment_id}")
def delete_deployment(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Unified delete — destroys the namespace of a pod deployment first.

    Restricted to the owner-view (creator, teacher, admin). Members
    can read-access the deployment but never tear it down.
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )

    # Destructive operation — uses the operate gate, which is
    # owner-or-admin only. Course-teachers explicitly do NOT get
    # delete/destroy rights on deployments in their courses; they only
    # get inspect (logs, infra).
    ensure_operate_deployment(current_user, deployment, db)

    # Per-deployment advisory lock — serialises against any concurrent
    # POST /pause, /resume or DELETE on the same deployment so the
    # ``current_status`` read below and the eventual
    # ``prepare_task_in_tx`` insert see a consistent picture. Without
    # this, two concurrent destroys could both pass the in-flight check
    # and one would crash on the partial unique index.
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)

    current_status = crud_deployments.get_deployment_status(db, deployment_id)

    # Active task in flight — neither path is safe.
    if current_status in lifecycle_service.IN_FLIGHT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Cannot delete a deployment in status '{current_status}'. "
                "Wait for the active task to finish."
            ),
        )

    # A pod deployment always goes through a destroy: even a cancelled one
    # may have created its namespace before the cancel arrived, and hiding
    # the row would leave the namespace running with nobody to see it. The
    # listener soft-deletes the row when the destroy task succeeds.
    if deployment.runtime == RUNTIME_K8S:
        return _dispatch_destroy(db, deployment, current_user)

    # A row of the removed OpenStack VM path: nothing of it is managed
    # any more, so hide it.
    success = crud_deployments.soft_delete_deployment(db, deployment_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# The job each task type runs, on the ``pods`` queue (the pod deployer, no
# OpenTofu, no root); see ``app.celery_app``.
_TASK_NAMES = {
    TaskType.DEPLOY: "pods.deploy_application",
    TaskType.DESTROY: "pods.destroy_deployment",
    TaskType.PAUSE: "pods.pause_deployment",
    TaskType.RESUME: "pods.resume_deployment",
    TaskType.REDEPLOY: "pods.redeploy_resource",
}


def _task_name(task_type: TaskType) -> str:
    return _TASK_NAMES[task_type]


def _render_teams(db: Session, teams) -> list[RenderTeam]:
    """The request's teams as the pod renderer sees them: members by user id and user name."""
    out = []
    for team in teams:
        members = []
        for raw in team.userIds:
            user = crud_users.get_user(db, UUID(str(raw)))
            if user is not None:
                members.append(Member(user_id=str(user.userId), name=user.username or user.email))
        out.append(RenderTeam(team.name, tuple(members)))
    return out


def _check_pod_workloads(db: Session, owner: User, loaded_spec, teams) -> int:
    """The number of workloads a pod deployment will run; refuses what the platform does not allow.

    422 ``team_names_collide`` when two team names normalise to the same
    workload name, 422 ``no_workloads`` without members to run anything for,
    422 ``too_many_workloads`` above ``K8S_MAX_WORKLOADS_PER_DEPLOYMENT``, 409
    ``workload_budget_exceeded`` when the owner's live pod deployments would
    exceed ``K8S_MAX_WORKLOADS_PER_OWNER``.
    """
    try:
        refs = workload_refs(loaded_spec.spec, _render_teams(db, teams))
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "team_names_collide", "message": str(e)},
        ) from None
    if not refs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "no_workloads", "message": "Add teams (and members, for per-person apps)"},
        )
    limit = settings.K8S_MAX_WORKLOADS_PER_DEPLOYMENT
    if len(refs) > limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "too_many_workloads", "workloads": len(refs), "limit": limit},
        )
    budget = settings.K8S_MAX_WORKLOADS_PER_OWNER
    if budget:
        used = crud_deployments.count_live_workloads(db, owner.userId)
        if used + len(refs) > budget:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": "workload_budget_exceeded", "in_use": used, "requested": len(refs), "budget": budget},
            )
    return len(refs)


def _check_pod_files(loaded_spec, user_input: dict | None) -> None:
    """413 unless the uploaded files of a pod app fit where they go.

    The pod worker puts every file a workload gets into one Kubernetes
    Secret, which holds at most 1 MiB, and each file variable may declare a
    smaller ``maxSize``. The deploy would otherwise fail late, in the
    cluster, with an error nobody can act on. Conservative: the largest slot
    of every file variable is counted once.
    """
    terraform_block = (user_input or {}).get("terraform") or {}
    total = 0
    for variable in loaded_spec.spec.variables:
        if variable.type != "file":
            continue
        limit = parse_size_bytes(variable.max_size) if variable.max_size else MAX_FILE_VARIABLE_BYTES
        largest = 0
        for slot_key, entry in (terraform_block.get(variable.name) or {}).items():
            if isinstance(entry, dict) and isinstance(entry.get("uploaded"), dict):
                entry = entry["uploaded"]
            size = int(entry.get("size") or 0) if isinstance(entry, dict) else 0
            if size > limit:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "reason": "file_too_large",
                        "variable": variable.name,
                        "slot": slot_key,
                        "limit_bytes": limit,
                        "actual_bytes": size,
                    },
                )
            largest = max(largest, size)
        total += largest
    if total > MAX_FILE_VARIABLE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"reason": "deployment_files_too_large", "limit_bytes": MAX_FILE_VARIABLE_BYTES},
        )


def _validate_teams(db: Session, teams) -> None:
    """422 unless every team has a unique, non-empty name and every member
    exists and is in exactly one team (.github#7 F).

    A user id that matched nobody used to be dropped silently, and a person
    in two teams got two workloads (or two VM accounts).
    """
    names = [team.name.strip() for team in teams]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "invalid_teams", "message": "Team names must be unique and not empty"},
        )
    seen: set[str] = set()
    for team in teams:
        for raw in team.userIds:
            try:
                user_id = UUID(str(raw))
            except ValueError:
                user_id = None
            if user_id is None or crud_users.get_user(db, user_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"reason": "unknown_team_member", "user_id": str(raw)},
                )
            if str(user_id) in seen:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail={"reason": "member_in_several_teams", "user_id": str(user_id)},
                )
            seen.add(str(user_id))


def _member_entry(user: User) -> dict:
    """One team member as the worker gets it: e-mail, user id, username."""
    return {"email": user.email, "user_id": str(user.userId), "username": user.username}


def _job_kwargs(deployment, user_vars: dict, teams: dict, **extra) -> dict:
    """The keyword arguments every job of ``deployment`` receives.

    ``commit_sha`` is the commit the deployment was created with; the
    worker fetches exactly it and never resolves a tag (.github#7 G).
    """
    return {
        "deployment_id": str(deployment.deploymentId),
        "app_id": str(deployment.appId),
        "app_git_link": deployment.app.git_link,
        "release": deployment.releaseTag,
        "commit_sha": deployment.commit_sha,
        "user_vars": user_vars,
        "teams": teams,
        **extra,
    }


def _commit_and_dispatch(
    db: Session,
    *,
    deployment_id,
    task_type: TaskType,
    celery_task_name: str,
    celery_kwargs: dict,
    dispatch_error_label: str,
    rollback_on_conflict: bool,
    after_task_id=None,
):
    """Insert the PENDING task in-TX, commit, then dispatch to Celery.

    Shared tail of the create and lifecycle dispatch paths:

    * ``prepare_task_in_tx`` → ``HTTPException(409)`` on an active task
      (the create path additionally rolls back its pending insert);
    * ``commit`` + ``refresh`` the task row;
    * ``dispatch_to_celery`` → ``HTTPException(503, dispatch_error_label)``
      on a Celery send failure (the task row is flipped to FAILED inside
      ``dispatch_to_celery`` in a fresh TX).

    ``after_task_id`` parks the message until that task's worker has let
    go of it (cancel → destroy).

    Returns the committed, dispatched ``Task``.
    """
    try:
        task = task_service_module.prepare_task_in_tx(
            db,
            deployment_id=deployment_id,
            task_type=task_type,
        )
    except task_service_module.ActiveTaskExistsError:
        if rollback_on_conflict:
            db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Deployment already has an active task",
        )
    db.commit()
    db.refresh(task)

    try:
        task, _celery_id = task_service_module.dispatch_to_celery(
            db,
            task=task,
            celery_task_name=celery_task_name,
            celery_kwargs=celery_kwargs,
            after_task_id=after_task_id,
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=dispatch_error_label,
        )
    return task


def _dispatch_destroy(db: Session, deployment, current_user: User):
    """Enqueue the destroy worker task for a deployment.

    Thin wrapper around :func:`_dispatch_lifecycle_task` so DELETE can
    keep its existing two-call sites (success-path / failed-path) clear.
    """
    return _dispatch_lifecycle_task(
        db,
        deployment,
        current_user,
        task_type=TaskType.DESTROY,
        response_status="destroying",
    )


def _dispatch_lifecycle_task(
    db: Session,
    deployment,
    current_user: User,
    task_type: TaskType,
    response_status: str,
    extra_kwargs: dict | None = None,
    after_task_id=None,
):
    """Enqueue any post-deploy lifecycle worker task for a deployment.

    Used by destroy, pause, resume, and per-workload redeploy — all four
    follow the same pattern: load user inputs, gather team membership,
    atomically insert a
    PENDING task row, commit, then ``send_task`` outside the locked
    TX. On a Celery send failure the task row flips to FAILED in a
    fresh TX (handled inside ``dispatch_to_celery``) and we surface a
    503 so the user sees an obvious failure instead of a permanent
    in-flight status.

    Args:
        task_type:           the ``TaskType`` enum value that drives both
                             the task row's ``type`` column and the
                             status the partial-unique index prevents
                             from coexisting.
        response_status:     synthetic deployment status returned to the
                             frontend in the 202 body — frontend uses
                             this to immediately switch the UI into the
                             live-stream view without re-fetching.
        extra_kwargs:        task-specific keyword arguments on top of the
                             common payload (``_job_kwargs``), e.g. the
                             ``resource_address`` of a redeploy.
        after_task_id:       park the task until that task's worker let
                             go of it (destroy after cancel).
    """
    try:
        user_vars = json.loads(deployment.userInputVar) if deployment.userInputVar else {}
    except Exception:
        user_vars = {}

    # Belt + braces: destroy, pause and resume do not need the uploaded file
    # contents; keep them out of the job message. Redeploy and deploy do
    # (a reset recreates the files of the workload).
    if task_type in (TaskType.DESTROY, TaskType.PAUSE, TaskType.RESUME):
        terraform_block = user_vars.get("terraform")
        if isinstance(terraform_block, dict):
            user_vars = {
                **user_vars,
                "terraform": {
                    k: v
                    for k, v in terraform_block.items()
                    if not looks_like_file_var(v)
                },
            }

    teams_dict: dict = {}
    if deployment.teams:
        # Persisted Team rows expose membership via the ``user_to_teams``
        # association, not a flat ``userIds`` field — that lives on the
        # request-side Pydantic schema in the create endpoint, not on
        # the ORM. ``get_team_members`` does the join for us.
        for team in deployment.teams:
            members = crud_deployments.get_team_members(db, team.teamId)
            teams_dict[team.name] = [_member_entry(m) for m in members]


    task = _commit_and_dispatch(
        db,
        deployment_id=deployment.deploymentId,
        task_type=task_type,
        celery_task_name=_task_name(task_type),
        celery_kwargs=_job_kwargs(deployment, user_vars, teams_dict, **(extra_kwargs or {})),
        dispatch_error_label=f"Could not dispatch {task_type.value} task — please retry",
        rollback_on_conflict=False,
        after_task_id=after_task_id,
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"task_id": str(task.taskId), "status": response_status},
    )


# ----------------------------------------------------------------
# WORKLOADS (live status of a pod deployment)
# ----------------------------------------------------------------
@router.get(
    "/{deployment_id}/resources",
    response_model=DeploymentResourceListResponse,
)
def list_deployment_resources(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """The workloads of a pod deployment, read live from its namespace.

    Inspect-only view: owner, admin, or a course-teacher of the
    deployment-owner's course. Course-teachers explicitly do NOT have
    operate rights; this endpoint is read-only.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    ensure_view_deployment_owner(current_user, deployment, db)

    if deployment.runtime != RUNTIME_K8S:
        # A row of the removed OpenStack VM path: there is nothing to show.
        return DeploymentResourceListResponse(runtime=deployment.runtime, workloads=[], live=False)

    # The namespace is the state; nothing of it is cached here.
    rows = k8s_status.workload_views(deployment_id, deployment.k8s_namespace or "")
    return DeploymentResourceListResponse(
        runtime=RUNTIME_K8S,
        workloads=[PodWorkloadSchema.model_validate(r) for r in rows or []],
        live=rows is not None,
    )


# A workload name as the pod renderer makes them (DNS-1123 label).
_WORKLOAD_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _dispatch_workload_action(db: Session, deployment_id: UUID, workload: str, current_user: User, *, reset: bool):
    """Restart or reset one workload of a pod deployment (202 ``redeploying``).

    Operate right; only from ``success`` or ``redeploy_failed`` (lifecycle,
    under the deployment lock). 422 for a row of the removed VM path or a malformed name,
    404 ``workload_not_found`` when the cluster knows no such workload of
    this deployment (the worker checks again when the cluster cannot be
    asked here).
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    ensure_operate_deployment(current_user, deployment, db)
    if deployment.runtime != RUNTIME_K8S:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "unsupported_runtime", "message": "Only pod deployments have workloads"},
        )
    if not _WORKLOAD_RE.match(workload):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "invalid_workload"},
        )
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)
    lifecycle_service.ensure_action_allowed(db, deployment, lifecycle_service.DeploymentAction.REDEPLOY)
    known = k8s_status.workload_views(deployment_id, deployment.k8s_namespace or "")
    if known is not None and workload not in {r["workload"] for r in known}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "workload_not_found", "workload": workload},
        )
    return _dispatch_lifecycle_task(
        db=db,
        deployment=deployment,
        current_user=current_user,
        task_type=TaskType.REDEPLOY,
        response_status="redeploying",
        extra_kwargs={"resource_address": f"reset:{workload}" if reset else workload},
    )


@router.post("/{deployment_id}/workloads/{workload}/restart", status_code=status.HTTP_202_ACCEPTED)
def restart_workload(
    deployment_id: UUID,
    workload: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Restart one team's or person's workload (a new pod); its data stays."""
    return _dispatch_workload_action(db, deployment_id, workload, current_user, reset=False)


@router.post("/{deployment_id}/workloads/{workload}/reset", status_code=status.HTTP_202_ACCEPTED)
def reset_workload(
    deployment_id: UUID,
    workload: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Reset one workload: its volume is deleted, it starts from scratch."""
    return _dispatch_workload_action(db, deployment_id, workload, current_user, reset=True)


# ----------------------------------------------------------------
# CANCEL DEPLOYMENT
# ----------------------------------------------------------------
#
# The only action permitted while a worker task is still in flight.
# Cancel is not "forget about it" - it is stop-and-clean, in this order,
# all in one transaction (.github#5, no Celery revoke any more):
#
#   1. Mark the in-flight task CANCELLED with ``cancel_requested_at`` and a
#      terminal ``task-revoked`` event, so open streams end and the
#      partial unique index admits the destroy task inserted below.
#   2. A queue message no worker has claimed yet is deleted: the job never
#      starts. Otherwise ``NOTIFY task_cancel`` reaches the worker running
#      it, which lets go of it.
#   3. Dispatch a destroy, which removes the namespace with everything the
#      deploy created. While a worker still holds the cancelled job the
#      destroy is parked behind it, so the two never run on the same
#      deployment at once (security review A-5, race 2).
@router.post("/{deployment_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def cancel_deployment(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Stop an in-flight deployment and clean up everything it created.

    Owner-only, same gate as Destroy. Returns ``202 + {task_id,
    status: "destroying"}`` so the frontend can stay on the live SSE
    view and watch the cleanup run.
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )

    ensure_operate_deployment(current_user, deployment, db)
    # Same locking discipline as pause/resume: hold the advisory lock
    # across the status check AND the task insert.
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)
    lifecycle_service.ensure_action_allowed(
        db, deployment, lifecycle_service.DeploymentAction.CANCEL,
    )

    blocking_task_id = None
    in_flight = crud_deployments.get_latest_task(db, deployment_id)
    if in_flight is not None and in_flight.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
        in_flight.status = TaskStatus.CANCELLED
        in_flight.cancel_requested_at = datetime.now(UTC)
        in_flight.finished_at = utcnow()
        unclaimed = db.execute(
            text("DELETE FROM celery_queue WHERE task_id = :id AND claimed_by IS NULL"),
            {"id": in_flight.taskId},
        ).rowcount
        if not unclaimed:
            # A worker has (or had) the job: the destroy waits for it.
            blocking_task_id = in_flight.taskId
        task_events.add_event(
            db,
            in_flight.taskId,
            EVENT_REVOKED,
            terminal_payload(
                EVENT_REVOKED,
                deployment_id=str(deployment_id),
                task_id=str(in_flight.taskId),
                task_type=in_flight.type.value if in_flight.type else None,
            ),
        )
        # Delivered with the commit inside the dispatch below.
        db.execute(text("SELECT pg_notify(:channel, :task_id)"), {
            "channel": NOTIFY_TASK_CANCEL,
            "task_id": str(in_flight.taskId),
        })
        db.flush()

    return _dispatch_lifecycle_task(
        db,
        deployment,
        current_user,
        task_type=TaskType.DESTROY,
        response_status="destroying",
        after_task_id=blocking_task_id,
    )


@router.post("/{deployment_id}/pause", status_code=status.HTTP_202_ACCEPTED)
def pause_deployment(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Pause a running deployment by scaling its workloads to zero.

    Owner-only — same gate as Destroy, because pausing a teammate's
    deployment is in practice a denial-of-service against the team.

    Returns ``202 + {task_id, status: "pausing"}`` on dispatch. The
    frontend reads ``status`` to switch to the live SSE view; the
    deployment's effective status is recomputed from the new task
    row by ``crud_deployments.get_deployment_status``.
    """
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )

    ensure_operate_deployment(current_user, deployment, db)
    # Hold the per-deployment advisory lock across the status check
    # AND the task insert so a parallel POST /pause can't sneak past
    # ``ensure_action_allowed`` between our read and the
    # ``prepare_task_in_tx`` flush.
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)
    lifecycle_service.ensure_action_allowed(
        db, deployment, lifecycle_service.DeploymentAction.PAUSE,
    )

    return _dispatch_lifecycle_task(
        db,
        deployment,
        current_user,
        task_type=TaskType.PAUSE,
        response_status="pausing",
    )


# ----------------------------------------------------------------
# RESUME DEPLOYMENT
# ----------------------------------------------------------------
#
# Reverses Pause. Allowed only on ``status='paused'`` — a deployment
# that was never paused has nothing to resume, so the lifecycle
# matrix gates this strictly. Returns 202 with the same shape as
# Pause/Destroy so the frontend handles all three the same way.
@router.post("/{deployment_id}/resume", status_code=status.HTTP_202_ACCEPTED)
def resume_deployment(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Resume a paused deployment by starting its workloads again."""
    deployment = crud_deployments.get_deployment_with_details(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )

    ensure_operate_deployment(current_user, deployment, db)
    # Resuming starts the app again: not while an admin has deactivated it.
    if deployment.app is not None:
        ensure_deploy_app(current_user, deployment.app)
    # Per-deployment advisory lock — same justification as in
    # ``pause_deployment`` above: keep the status check and the task
    # insert atomic against concurrent /resume / /pause / DELETE
    # requests on this deployment.
    crud_locks.acquire_deployment_xact_lock(db, deployment_id)
    lifecycle_service.ensure_action_allowed(
        db, deployment, lifecycle_service.DeploymentAction.RESUME,
    )

    return _dispatch_lifecycle_task(
        db,
        deployment,
        current_user,
        task_type=TaskType.RESUME,
        response_status="resuming",
    )


# ----------------------------------------------------------------
# DOWNLOAD UPLOADED FILE
# ----------------------------------------------------------------
#
# Lets the deployment owner re-fetch a file they uploaded at deploy
# time. The list / detail endpoints strip the base64 payload so they
# don't ship megabytes per page render; this endpoint is the only
# path that returns the actual bytes. Owner-only — members can see
# that a file was uploaded (metadata survives the strip), but the
# bytes themselves stay restricted to whoever created the deployment.
@router.get(
    "/{deployment_id}/files/{var_name}/{slot_key}",
    response_class=Response,
)
def download_deployment_file(
    deployment_id: UUID,
    var_name: str,
    slot_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Stream the raw bytes of one wizard-uploaded file back to the owner.

    Path components mirror how the upload was indexed:
      * ``var_name`` — the ``@openstack:file:*`` variable name
      * ``slot_key`` — the inner-map key (``"all"`` for scope=all,
        team name for scope=team, ``Team-User`` composite for scope=user)

    Returns 404 if any layer of the lookup misses; the frontend can
    therefore probe a slot's existence via this endpoint without
    needing a separate metadata response.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    # Inspect-only view, gated through capabilities so course-teachers
    # can download the same wizard-uploaded files they can already see
    # referenced in the inspect view (logs / detail). Owners + admins
    # keep their access. The list/detail strip-pass already hid the
    # base64 payload from plain members, so this endpoint stays
    # restricted to the owner-view set.
    ensure_view_deployment_owner(current_user, deployment, db)

    if not deployment.userInputVar:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No files")
    try:
        user_input = json.loads(deployment.userInputVar)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No files")

    tf_block = user_input.get("terraform") if isinstance(user_input, dict) else None
    var_value = (tf_block or {}).get(var_name)
    if not looks_like_file_var(var_value):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No uploaded file under variable '{var_name}'",
        )
    # ``var_value`` is ``{slot_key: {name, content_b64, size, content_type}}``;
    # a team- or user-scoped variable wraps each entry as ``{"uploaded": …}``
    # (its HCL type is a map of maps).
    entry = var_value.get(slot_key)
    if isinstance(entry, dict) and isinstance(entry.get("uploaded"), dict):
        entry = entry["uploaded"]
    if not isinstance(entry, dict) or "content_b64" not in entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No file in slot '{slot_key}'",
        )

    try:
        payload = base64.b64decode(entry["content_b64"], validate=True)
    except (binascii.Error, ValueError):
        # Persisted bytes are corrupt — surface as 500 because there's
        # nothing the caller can do; this is a server-side data bug.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored file payload is not valid base64",
        )

    # Never the uploader's content type, and a name that cannot break the
    # header: the bytes are served for saving, not for the browser to render
    # (.github#7 R).
    filename = str(entry.get("name") or f"{var_name}-{slot_key}")
    return Response(
        content=payload,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(filename),
            "Content-Length": str(len(payload)),
            "X-Content-Type-Options": "nosniff",
        },
    )


# ----------------------------------------------------------------
# GET OWN ACCESS CREDENTIALS (member self-service)
# ----------------------------------------------------------------
@router.get("/{deployment_id}/my-access", response_model=MyAccessResponse)
def get_my_access(
    deployment_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Return the calling member's OWN access credentials for a deployment.

    The only way to the credentials (no mails, .github#7 I). The full
    outputs carry every teammate's credentials and are owner-view only;
    a team member — typically a student — gets exactly their own account
    entry, matched exactly by ``services.access`` (.github#7 H), plus their
    team's VM block. Never a teammate's.

    Member-view gate: owner, team member or direct mapping; anyone else
    gets 403.

    Returns 200 with empty maps when there's no successful deploy yet or
    no entry belongs to the caller exactly — the UI renders a "no
    credentials yet" state rather than treating it as an error.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deployment not found",
        )
    ensure_view_deployment_member(current_user, deployment, db)

    result = access.get_user_access(db, deployment_id, current_user.userId)
    if result is None:
        return MyAccessResponse()
    return MyAccessResponse(
        user_accounts=result.get("user_accounts", {}),
        team_vms=result.get("team_vms", {}),
    )


# ----------------------------------------------------------------
# LIVE STREAM — Server-Sent Events for progress + log tail
# ----------------------------------------------------------------
#
# Several kinds of events flow through the stream:
#
# * ``event: snapshot`` — fired once at connect with the latest task's
#   current_phase / progress_pct / status. Lets a freshly-loaded page
#   render the bar at the right position before the worker emits its
#   next progress update.
# * ``event: progress`` — every ``task-progress`` from the worker. The
#   payload includes ``phase``, ``phase_index``, ``total_phases``,
#   ``progress_pct``, ``message``.
# * ``event: log`` — every ``task-log`` from the worker. The payload
#   is the LogEntry dict (timestamp, level, category, message, plus
#   tool/streaming flags for streaming subprocess lines).
# * ``event: succeeded`` / ``failed`` / ``revoked`` — the task ended.
# * comment lines starting with ``:`` are SSE keepalive pings.
#
# The events come from ``task_events`` (.github#5): the worker appends
# them, any API replica can serve any stream. Each frame carries the row
# id as its SSE ``id``; a client that reconnects with ``Last-Event-ID``
# resumes right after it. Without one it gets the tail of the transcript
# first. New rows are announced by ``NOTIFY task_events``
# (``task_events.hub``), with a slow poll as fallback.
#
# The stream stays open until the task reaches a terminal state, the
# client disconnects, or the backend shuts down.
@router.get("/{deployment_id}/stream")
async def stream_deployment_events(
    deployment_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_keycloak),
):
    """Live progress + log stream for one deployment as Server-Sent Events.

    Streams the events of the deployment's latest task and ends with its
    terminal event. Starts with a ``snapshot`` event; for a task that is
    already finished (or no task at all) the stream ends right after it.
    """
    deployment = crud_deployments.get_deployment(db, deployment_id)
    if not deployment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    # Inspect-only view via capabilities. The live stream surfaces
    # task-log lines (raw worker stdout incl. terraform output, packer
    # build chatter, etc.); course-teachers of the deployment-owner's
    # course are in the inspect set, owners and admins keep their access,
    # and plain members still see metadata only.
    ensure_view_deployment_owner(current_user, deployment, db)

    latest_task = crud_deployments.get_latest_task(db, deployment_id)
    snapshot_payload = {
        "task_id": str(latest_task.taskId) if latest_task else None,
        "status": latest_task.status.value if latest_task else None,
        "current_phase": getattr(latest_task, "current_phase", None),
        "progress_pct": getattr(latest_task, "progress_pct", None),
        "type": latest_task.type.value if latest_task else None,
    }
    initial_status = latest_task.status if latest_task else None
    task_id = latest_task.taskId if latest_task else None
    # A successful destroy soft-deletes the deployment; the client reloads
    # when the stream ends and must find it gone. Other follow-up work
    # does not need to hold up the terminal event.
    finalize_first = latest_task is not None and latest_task.type == TaskType.DESTROY
    try:
        last_event_id = int(request.headers.get("last-event-id") or 0) or None
    except ValueError:
        last_event_id = None

    async def event_stream() -> AsyncIterator[bytes]:
        yield _sse_frame("snapshot", snapshot_payload)
        # Nothing will happen for a finished task (or a deployment without
        # one): the client renders the static logs instead.
        if task_id is None or initial_status in _FINISHED_STATUSES:
            return

        loop = asyncio.get_running_loop()
        with task_events.hub.subscribe(task_id) as wake:
            rows, finished = await asyncio.to_thread(_stream_rows, task_id, last_event_id)
            keepalive_at = loop.time() + _SSE_KEEPALIVE_SECONDS
            after_id = last_event_id or 0
            while True:
                for event_id, event_type, payload in rows:
                    after_id = max(after_id, event_id)
                    if event_type in TERMINAL_EVENTS:
                        if finalize_first:
                            await asyncio.to_thread(_finalize, task_id)
                        yield _sse_frame(_event_name_for(event_type), payload, event_id=event_id)
                        return
                    yield _sse_frame(_event_name_for(event_type), payload, event_id=event_id)
                if finished and not rows:
                    # Finished without a terminal event on record (e.g. the
                    # dispatch failed): close with one built from the row.
                    terminal = await asyncio.to_thread(_terminal_from_row, task_id)
                    if terminal is not None:
                        yield _sse_frame(_event_name_for(terminal[0]), terminal[1])
                    return
                if not rows:
                    poll = _SSE_POLL_SECONDS if task_events.hub.listening else _SSE_FALLBACK_POLL_SECONDS
                    timeout = min(poll, max(keepalive_at - loop.time(), 0.0))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(wake.wait(), timeout=timeout)
                    wake.clear()
                if await request.is_disconnected():
                    return
                now = loop.time()
                if now >= keepalive_at:
                    # Proxies and EventSource close idle connections.
                    yield b": keepalive\n\n"
                    keepalive_at = now + _SSE_KEEPALIVE_SECONDS
                rows, finished = await asyncio.to_thread(_stream_rows, task_id, after_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx response buffering
            "Connection": "keep-alive",
        },
    )


_FINISHED_STATUSES = (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)
_SSE_KEEPALIVE_SECONDS = 15.0
# With the event hub listening, new rows are announced; the poll only
# covers a missed notification. Without it (hub reconnecting) poll faster.
_SSE_POLL_SECONDS = 5.0
_SSE_FALLBACK_POLL_SECONDS = 1.0


def _stream_rows(task_id: UUID, after_id: int | None) -> tuple[list[tuple[int, str, dict]], bool]:
    """Events to send next and whether the task has finished.

    ``after_id`` None means a fresh connection: the tail of the
    transcript. Runs in a worker thread with its own session, because the
    stream outlives the request's session.
    """
    with SessionLocal() as session:
        if after_id is None:
            events = task_events.backfill(session, task_id)
        else:
            events = task_events.events_after(session, task_id, after_id)
        status_value = session.execute(
            select(TaskModel.status).where(TaskModel.taskId == task_id)
        ).scalar()
    finished = status_value in _FINISHED_STATUSES
    return [(event.id, event.type, event.payload) for event in events], finished


def _finalize(task_id: UUID) -> None:
    """Run the task's follow-up work now (no-op if another process already did)."""
    with SessionLocal() as session:
        task_finalizer.finalize_task(session, task_id)


def _terminal_from_row(task_id: UUID) -> tuple[str, dict] | None:
    """A terminal event built from the task row, for tasks that ended without one."""
    with SessionLocal() as session:
        task = session.get(TaskModel, task_id)
        if task is None or task.status not in _FINISHED_STATUSES:
            return None
        event_type = _TERMINAL_EVENT_BY_STATUS[task.status]
        return event_type, terminal_payload(
            event_type,
            deployment_id=str(task.deploymentId),
            task_id=str(task.taskId),
            task_type=task.type.value if task.type else None,
        )


_TERMINAL_EVENT_BY_STATUS = {
    TaskStatus.SUCCESS: EVENT_SUCCEEDED,
    TaskStatus.FAILED: EVENT_FAILED,
    TaskStatus.CANCELLED: EVENT_REVOKED,
}

_EVENT_NAME_MAP: dict[str, str] = {
    "task-progress": "progress",
    "task-log": "log",
    "task-overflow": "overflow",
    "task-started": "started",
    "task-succeeded": "succeeded",
    "task-failed": "failed",
    "task-revoked": "revoked",
}


def _event_name_for(event_type: str | None) -> str:
    """Map stored event types onto short SSE event names.

    Frontend code attaches listeners by these short names rather than
    the verbose event types; ``_EVENT_NAME_MAP`` is the single source
    of truth on both sides of the wire.
    """
    return _EVENT_NAME_MAP.get(event_type or "", "message")


def _sse_frame(event_name: str, payload: dict, event_id: int | None = None) -> bytes:
    """Serialise one SSE frame.

    SSE format:

    ```
    id: <event id>\n        (stored events only)
    event: <name>\n
    data: <json>\n
    \n
    ```

    Embedded newlines in the JSON would split the frame into multiple
    ``data:`` lines per the SSE spec; we use ``json.dumps`` defaults
    which keep everything on one line.
    """
    body = json.dumps(payload, default=str)
    id_line = f"id: {event_id}\n" if event_id is not None else ""
    return f"{id_line}event: {event_name}\ndata: {body}\n\n".encode()
