import json
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import AppVersionApprovalStatus, TaskStatus, TaskType, UserRole


# ----------------------------------------------------------------
# USER SCHEMAS
# ----------------------------------------------------------------
class UserBase(BaseModel):
    email: EmailStr
    username: str


class UserCreate(UserBase):
    role: UserRole = UserRole.STUDENT
    courseId: UUID | None = None


class UserUpdate(BaseModel):
    # Profile fields are managed exclusively through Keycloak; the only
    # update path here is ``role`` and it is admin-only.
    role: UserRole | None = None
    courseId: UUID | None = None


class UserResponse(UserBase):
    # Output only: the address was validated when the user was created or
    # synced from Keycloak. Re-validating it strictly here made every response
    # embedding the user fail with HTTP 500 for reserved domains (.invalid,
    # .test, example.test), which email-validator rejects.
    email: str
    userId: UUID
    role: UserRole
    courseId: UUID | None = None
    keycloak_id: str | None = None
    firstName: str | None = None
    lastName: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserWithCourse(UserResponse):
    course: Optional["CourseResponse"] = None

    model_config = ConfigDict(from_attributes=True)


class Token(BaseModel):
    access_token: str
    token_type: str


# ----------------------------------------------------------------
# COURSE SCHEMAS
# ----------------------------------------------------------------
class CourseBase(BaseModel):
    name: str


class CourseCreate(CourseBase):
    pass


class CourseUpdate(BaseModel):
    name: str | None = None


class CourseResponse(CourseBase):
    courseId: UUID

    model_config = ConfigDict(from_attributes=True)


class CourseWithUsers(CourseResponse):
    users: list[UserResponse] = []

    model_config = ConfigDict(from_attributes=True)


class CourseMembersUpdate(BaseModel):
    """Body for bulk-adding members to a course.

    Course membership lives on ``users.courseId``, so a "member" is a
    user-id whose FK is (re-)pointed to this course; an existing
    enrollment on another course is overwritten.
    """

    userIds: list[UUID] = []


# ----------------------------------------------------------------
# APP SCHEMAS
# ----------------------------------------------------------------
class AppBase(BaseModel):
    name: str
    description: str | None = None
    git_link: str | None = None
    is_private: bool = False


class AppCreate(AppBase):
    # Image is sent as a full data-URL string, e.g.
    # ``"data:image/png;base64,iVBORw0KG..."``. The router decodes the
    # base64 part to bytes and stores mime + bytes in two columns.
    image: str | None = None
    # When True and is_private=False, all existing Git tags are
    # automatically submitted for review right after creation.
    submit_all_versions: bool = False


class AppUpdate(BaseModel):
    # ``git_link`` is intentionally NOT editable — once an app has
    # deployments, changing the repo would make the version history
    # inconsistent. Callers may include it; it is silently ignored.
    name: str | None = None
    description: str | None = None
    is_private: bool | None = None
    # Same data-URL convention as ``AppCreate``. Pass ``""`` to clear
    # the image; ``None`` (the default) leaves it unchanged.
    image: str | None = None

    model_config = ConfigDict(extra="ignore")


class AppResponse(AppBase):
    appId: UUID
    userId: UUID
    created_at: datetime
    is_private: bool
    # An admin deactivated the app; only an admin can make it public again.
    hidden_by_admin: bool = False
    # Data-URL or null. Populated from ``app.image`` + ``app.image_mime``
    # by ``serialize_app_image``; the raw bytes never leave the backend.
    image: str | None = None

    model_config = ConfigDict(from_attributes=True)


class AppWithUser(AppResponse):
    user: UserResponse

    model_config = ConfigDict(from_attributes=True)


class AppWithVersions(AppWithUser):
    versions: list[dict[str, str]] = []

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# APP VERSION APPROVAL SCHEMAS
# ----------------------------------------------------------------
class AppVersionApprovalSubmit(BaseModel):
    diff_url: str | None = None
    notes: str | None = None


class AppVersionApprovalDecision(BaseModel):
    rejection_reason: str


class AppVersionApprovalResponse(BaseModel):
    approvalId: UUID
    appId: UUID
    version_tag: str
    # The reviewed commit; the approval does not cover the tag once it
    # moves (.github#7 G). None for reviews from before the pinning.
    commit_sha: str | None = None
    # How the version runs and the reviewed appstore.yaml (its SHA-256)
    # and image digests (.github#4).
    runtime: str = "kubernetes"
    spec_sha256: str | None = None
    image_digests: list[str] | None = None
    status: AppVersionApprovalStatus
    diff_url: str | None = None
    notes: str | None = None
    rejection_reason: str | None = None
    reviewed_by: UUID | None = None
    reviewed_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("image_digests", mode="before")
    @classmethod
    def _digests_from_json(cls, value):
        """Stored as a JSON list on the row."""
        if isinstance(value, str):
            return json.loads(value)
        return value


class AppVersionApprovalWithApp(AppVersionApprovalResponse):
    app: AppResponse

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# DEPLOYMENT SCHEMAS
# ----------------------------------------------------------------
class Team(BaseModel):
    name: str
    userIds: list[str] = []


class DeploymentBase(BaseModel):
    name: str
    appId: UUID


class DeploymentFileUpload(BaseModel):
    """Single file uploaded by the teacher in the deploy wizard.

    Persisted verbatim into ``userInputVar.terraform`` keyed by the
    variable name (and recipient key for team/user scope); the pod
    deployer puts it into the workload's files.

    ``content_b64`` MUST be standard (RFC 4648) base64.
    """

    name: str
    content_b64: str
    size: int
    content_type: str | None = None


class DeploymentCreate(DeploymentBase):
    # Required: the deployment runs the commit this tag points to now,
    # which must be the approved one for a public app (.github#7 G).
    releaseTag: str = Field(..., min_length=1)
    userInputVar: dict[str, Any] | None = None
    # Files-map keyed by the name of a ``file`` variable of the
    # appstore.yaml. Inner key:
    #   * scope = all  → exactly one inner key, conventionally "all"
    #   * scope = team → one entry per team name
    #   * scope = user → one entry per ``Team-User`` composite key
    # The backend doesn't auto-route — what the wizard puts in here
    # ends up 1:1 in the variable.
    files: dict[str, dict[str, DeploymentFileUpload]] | None = None
    teams: list[Team] = []


class DeploymentResponse(DeploymentBase):
    deploymentId: UUID
    userId: UUID
    releaseTag: str | None = None
    # The commit every job of the deployment runs.
    commit_sha: str | None = None
    runtime: str = "kubernetes"
    userInputVar: dict[str, Any] | None = None
    status: str | None = None  # From latest task
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class DeploymentWithRelations(DeploymentResponse):
    user: UserResponse
    app: AppResponse

    model_config = ConfigDict(from_attributes=True)


# Team response for deployment details (from DB)
class DeploymentTeamMember(BaseModel):
    userId: UUID
    email: str
    username: str

    model_config = ConfigDict(from_attributes=True)


class DeploymentTeamResponse(BaseModel):
    teamId: UUID
    name: str
    members: list[DeploymentTeamMember] = []

    model_config = ConfigDict(from_attributes=True)


# Task summary for deployment details
class TaskSummary(BaseModel):
    taskId: UUID
    type: TaskType
    status: TaskStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    # Live-progress fields (mirror of Task model), used to render the
    # progress bar on page reload; SSE supplies real-time updates.
    current_phase: str | None = None
    progress_pct: int | None = None

    model_config = ConfigDict(from_attributes=True)


# Outputs of a deployment
class DeploymentOutputs(BaseModel):
    """Outputs of the deploy job - structure depends on app"""

    raw: dict[str, Any] | None = None  # Full outputs as dict

    model_config = ConfigDict(from_attributes=True)


# Per-member self-access response
class MyAccessResponse(BaseModel):
    """A single team member's own access credentials.

    Returned by ``GET /deployments/{id}/my-access`` so a member (student)
    can see their OWN credentials without the owner-view that gates the
    full outputs. Only the caller's own account is ever present.

    Both maps use the ``user_accounts`` / ``team_vms`` output shape (one key
    each: the member's account and their team's workload)
    so the frontend's existing account-matching pipeline consumes this
    unchanged. Empty maps mean "no credentials yet" (no successful deploy,
    or the app issued no per-user account) — a valid 200, not an error.
    """

    user_accounts: dict[str, dict[str, Any]] = {}
    team_vms: dict[str, dict[str, Any]] = {}

    model_config = ConfigDict(from_attributes=True)


class DeploymentPermissions(BaseModel):
    """What the caller may do with a deployment, decided by the API.

    ``owner_view``: tasks, logs, the live stream, infrastructure and the
    (redacted) outputs. ``operate``: pause, resume, destroy, redeploy,
    cancel and team changes. ``member``: the caller is in one of its teams
    and gets their own access through ``my-access``.
    """

    owner_view: bool = False
    operate: bool = False
    member: bool = False


# Full deployment detail response
class DeploymentDetail(DeploymentWithRelations):
    """Full deployment details with teams, task info, and outputs"""

    teams: list[DeploymentTeamResponse] = []
    latest_task: TaskSummary | None = None
    outputs: DeploymentOutputs | None = None
    logs: str | None = None  # Optional: can be excluded for large logs
    permissions: DeploymentPermissions = DeploymentPermissions()
    # Lifecycle actions allowed right now (``services.lifecycle``), for a
    # caller with ``operate``: destroy, delete, pause, resume, cancel,
    # redeploy. Empty for everyone else.
    actions: list[str] = []

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# POD WORKLOAD SCHEMAS
# ----------------------------------------------------------------
class PodSetupProgress(BaseModel):
    """What an app reports about its first-start setup (an ``appstore-progress`` log line)."""

    percent: int
    step: str | None = None
    # Seconds the app expects to need still; null when it cannot tell yet.
    etaSeconds: int | None = None


class PodWorkloadSchema(BaseModel):
    """One workload of a pod deployment, read live from the cluster (.github#4)."""

    workload: str
    team: str | None = None
    # ``scope: user``: whose workload it is.
    user_id: str | None = None
    ready: bool = False
    restarts: int = 0
    phase: str
    url: str | None = None
    lastEvent: str | None = None
    # Set while the app runs a setup of its own (an installer) after it started.
    setup: PodSetupProgress | None = None


class DeploymentResourceListResponse(BaseModel):
    """Wrap the list in an object so we can add cursor/refresh-stamp
    metadata later without a breaking API change."""

    # Whether the cluster could be asked.
    live: bool
    runtime: str = "kubernetes"
    workloads: list[PodWorkloadSchema] = []


# ----------------------------------------------------------------
# Task SCHEMAS
# ----------------------------------------------------------------
class TaskBase(BaseModel):
    deploymentId: UUID
    celeryTaskId: str
    type: TaskType
    status: TaskStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    logs: str | None = None
    tf_state: str | None = None
    outputs: str | None = None
    current_phase: str | None = None
    progress_pct: int | None = None


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    status: TaskStatus | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    logs: str | None = None
    tf_state: str | None = None
    outputs: str | None = None
    current_phase: str | None = None
    progress_pct: int | None = None


class TaskResponse(BaseModel):
    """A task as the task API shows it to the owner view.

    Deliberately without ``tf_state`` and ``outputs`` (.github#7 E): the
    state holds every attribute of every resource, generated passwords
    included, and the outputs hold every member's credentials. Members get
    their own entry through ``my-access``; the owner view's outputs are
    redacted (``task_results.redact_sensitive_outputs``).
    """

    taskId: UUID
    deploymentId: UUID
    celeryTaskId: str | None = None
    type: TaskType
    status: TaskStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    logs: str | None = None
    current_phase: str | None = None
    progress_pct: int | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# TEAM SCHEMAS
#
# Teams are bound directly to a deployment (FK
# ``deployments.deploymentId`` -> ``Team.deploymentId``). The schemas
# mirror the model 1:1.
# ----------------------------------------------------------------
class TeamBase(BaseModel):
    name: str
    deploymentId: UUID


class TeamCreate(TeamBase):
    userIds: list[UUID] = []


class TeamUpdate(BaseModel):
    name: str | None = None


class TeamResponse(TeamBase):
    teamId: UUID

    model_config = ConfigDict(from_attributes=True)


class TeamWithMembers(TeamResponse):
    users: list[UserResponse] = []

    model_config = ConfigDict(from_attributes=True)


# ----------------------------------------------------------------
# STATISTICS SCHEMAS
# ----------------------------------------------------------------
class UserStatistics(BaseModel):
    total_apps: int
    total_deployments: int
    successful_deployments: int
    failed_deployments: int
    pending_deployments: int
