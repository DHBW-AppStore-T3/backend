import enum
import uuid

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.database import Base
from app.utils.time import utcnow

# How a deployment runs: pods in the cluster rendered from the app's
# appstore.yaml (.github#4), or KubeVirt VMs rendered from the app's
# appstore.yaml. The values are the ``runtime`` names of the appstore.yaml
# contract.
RUNTIME_K8S = "kubernetes"
RUNTIME_KUBEVIRT = "kubevirt-vm"
# Runtimes whose objects live in this cluster (no OpenStack, no OpenTofu state).
CLUSTER_RUNTIMES = (RUNTIME_K8S, RUNTIME_KUBEVIRT)


# ----------------------------------------------------------------
# ENUMS
# ----------------------------------------------------------------
class UserRole(str, enum.Enum):
    STUDENT = "student"
    TEACHER = "teacher"
    ADMIN = "admin"


class AppVersionApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TaskType(str, enum.Enum):
    DEPLOY = "deploy"
    UPDATE = "update"
    DESTROY = "destroy"
    PAUSE = "pause"
    RESUME = "resume"
    # Restart (or reset) of ONE workload, without touching the others of
    # the same deployment.
    REDEPLOY = "redeploy"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ----------------------------------------------------------------
# COURSE MODEL
# ----------------------------------------------------------------
class Course(Base):
    __tablename__ = "courses"

    courseId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    # The LTI context (``iss`` of the platform + ``context.id``) of a course an
    # LTI launch created or adopted; the name is only its label (review S9).
    lti_issuer = Column(String, nullable=True)
    lti_context_id = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("lti_issuer", "lti_context_id", name="uq_courses_lti_context"),
    )

    # Relationships
    users = relationship("User", back_populates="course")
    # Many-to-many to User via the ``course_teachers`` join table:
    # a course can have several teachers and a teacher several courses.
    # Backs the ``is_course_teacher`` capability check.
    course_teachers = relationship(
        "CourseTeacher",
        back_populates="course",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# USER MODEL
# ----------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    userId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    keycloak_id = Column(String, unique=True, index=True, nullable=True)  # Keycloak User ID (sub)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, nullable=False)
    firstName = Column(String, nullable=True)
    lastName = Column(String, nullable=True)
    # The effective role: the highest of the three sources below, recomputed
    # whenever one of them changes (app.utils.roles, review S8).
    role = Column(Enum(UserRole), nullable=False, default=UserRole.STUDENT)
    # What each source said last: the realm roles of the latest Keycloak
    # token, the role of the latest LTI launch, and an admin's grant
    # (``PUT /users/{id}``). NULL: that source never said anything.
    keycloak_role = Column(Enum(UserRole), nullable=True)
    lti_role = Column(Enum(UserRole), nullable=True)
    granted_role = Column(Enum(UserRole), nullable=True)
    # The LTI identity (``iss`` + ``sub`` of the platform's id_token) this
    # account is linked to; the e-mail is only an attribute (review S9).
    lti_issuer = Column(String, nullable=True)
    lti_subject = Column(String, nullable=True)
    courseId = Column(UUID(as_uuid=True), ForeignKey("courses.courseId"), nullable=True, index=True)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("lti_issuer", "lti_subject", name="uq_users_lti_identity"),)

    # Relationships
    course = relationship("Course", back_populates="users")
    apps = relationship("App", back_populates="user")
    deployments = relationship("Deployment", back_populates="user")
    user_to_deployments = relationship("UserToDeployment", back_populates="user")
    user_to_teams = relationship("UserToTeam", back_populates="user")
    course_teacher_links = relationship(
        "CourseTeacher",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# APP MODEL
# ----------------------------------------------------------------
class App(Base):
    __tablename__ = "apps"

    appId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    image = Column(LargeBinary, nullable=True)  # raw bytes of the uploaded logo
    image_mime = Column(
        String(64), nullable=True
    )  # e.g. "image/png" — needed to build a data-URL on read
    git_link = Column(String, nullable=True)
    is_private = Column(Boolean, nullable=False, default=False)
    # Set by an admin's emergency deactivation: the app is private and only
    # an admin may make it public again, so the owner cannot undo the
    # deactivation (.github#7 R, dhbw-appstore).
    hidden_by_admin = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    # Set by an admin: the app is highlighted in the catalog. Only the admin
    # endpoint ``PUT /admin/apps/{id}/recommended`` changes it.
    recommended = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    # The version tag teachers deploy; set by an admin, only to an approved
    # version. NULL: nothing set. Cleared when that version's approval ends
    # (revoked, or replaced by a new submission).
    deploy_version = Column(String, nullable=True)
    userId = Column(UUID(as_uuid=True), ForeignKey("users.userId"), nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow)
    # Soft-delete marker. When set, the app is hidden from default
    # queries but the row stays so existing deployments keep their FK
    # valid. Soft-delete is refused while the app has live deployments.
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # Default queries hide soft-deleted apps.
        Index("ix_apps_live", "appId", postgresql_where=text("deleted_at IS NULL")),
    )

    # Relationships
    user = relationship("User", back_populates="apps")
    deployments = relationship("Deployment", back_populates="app")
    version_approvals = relationship(
        "AppVersionApproval",
        back_populates="app",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# DEPLOYMENT MODEL
# ----------------------------------------------------------------
class Deployment(Base):
    __tablename__ = "deployments"

    deploymentId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    releaseTag = Column(String, nullable=True)
    # The commit ``releaseTag`` pointed to when the deployment was created.
    # Every job of the deployment runs exactly this commit (.github#7 G).
    commit_sha = Column(String(40), nullable=True)
    runtime = Column(String(16), nullable=False, default=RUNTIME_K8S, server_default=RUNTIME_K8S)
    # Pod deployments: their namespace (``dep-<first 8 hex of the id>``),
    # unique across all deployments, deleted ones included, so a destroy can
    # never reach another deployment's namespace; and how many workloads
    # (one per team or per person) they run, for the owner's budget.
    k8s_namespace = Column(String(63), nullable=True)
    k8s_workloads = Column(Integer, nullable=True)
    userInputVar = Column(Text, nullable=True)  # could also be JSON
    userId = Column(UUID(as_uuid=True), ForeignKey("users.userId"), nullable=False, index=True)
    appId = Column(UUID(as_uuid=True), ForeignKey("apps.appId"), nullable=False, index=True)
    # Soft-delete marker. Set to hide the deployment from default
    # queries while keeping the row for audit/restore. DELETE is only
    # allowed in terminal states.
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        # Default queries hide soft-deleted deployments.
        Index(
            "ix_deployments_live",
            "deploymentId",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_deployments_k8s_namespace",
            "k8s_namespace",
            unique=True,
            postgresql_where=text("k8s_namespace IS NOT NULL"),
        ),
    )

    # Relationships
    user = relationship("User", back_populates="deployments")
    app = relationship("App", back_populates="deployments")
    user_to_deployments = relationship(
        "UserToDeployment",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    teams = relationship(
        "Team",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    tasks = relationship(
        "Task",
        back_populates="deployment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# TASK MODEL
# ----------------------------------------------------------------
class Task(Base):
    __tablename__ = "tasks"

    taskId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    type = Column(Enum(TaskType), nullable=False)
    status = Column(Enum(TaskStatus), default=TaskStatus.PENDING)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    logs = Column(Text, nullable=True)  # JSON or text
    tf_state = Column(Text, nullable=True)  # Terraform state as JSON/text
    outputs = Column(Text, nullable=True)  # Terraform outputs as JSON/text
    # Live-progress columns, updated by the worker with each
    # ``task-progress`` event. Advisory only — the event stream is
    # canonical; these let a page reload show the last known phase/pct.
    current_phase = Column(String(50), nullable=True)
    progress_pct = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    # Queue columns (.github#5): the task row is the queue entry.
    # The registered job the worker runs (``worker/app/job_runtime.py``)
    # and its keyword arguments. The arguments may hold uploaded files: the
    # trigger ``tasks_drop_job_kwargs`` (below) drops them the moment the
    # task leaves PENDING.
    job = Column(String, nullable=True)
    job_kwargs = Column(JSONB, nullable=True)
    # Unlike the columns above these are timezone-aware and are compared
    # with ``now()`` in SQL.
    # The worker process running the task and how long its lease lasts;
    # the worker renews it while the job runs. A RUNNING task with an
    # expired lease belongs to a worker that died (reconciler: worker_lost).
    claimed_by = Column(Text, nullable=True)
    lease_until = Column(DateTime(timezone=True), nullable=True)
    # Set by POST /deployments/{id}/cancel; the worker stops the job.
    cancel_requested_at = Column(DateTime(timezone=True), nullable=True)
    # Follow-up work of a finished task (soft-delete after destroy, access
    # mails after deploy) ran; claimed once, by one API process.
    finalized_at = Column(DateTime(timezone=True), nullable=True)
    # Fernet token of {"terraform_outputs": ..., "tf_state": ...} as the
    # worker reported them (``app.services.task_results``). Replaces the
    # plaintext ``outputs``/``tf_state`` for tasks run since .github#5.
    outputs_enc = Column(LargeBinary, nullable=True)

    __table_args__ = (
        # At most one PENDING/RUNNING task per deployment: the database
        # backstop of ``task_service.enqueue_task_in_tx``. The enum stores
        # member names, hence the upper-case literals.
        Index(
            "uq_tasks_active_per_deployment",
            "deploymentId",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING')"),
        ),
    )

    # Relationships
    deployment = relationship("Deployment", back_populates="tasks")


# The trigger of migration f8b0c2d4e6a8, also for the schema ``create_all``
# builds (tests): a task's job arguments are dropped the moment it leaves
# PENDING, whoever changes its status (the worker's claim included).
event.listen(
    Task.__table__,
    "after_create",
    DDL(
        """
        CREATE OR REPLACE FUNCTION tasks_drop_job_kwargs() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.job_kwargs := NULL;
            RETURN NEW;
        END
        $$
        """
    ),
)
event.listen(
    Task.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER tasks_drop_job_kwargs BEFORE UPDATE OF status ON tasks
            FOR EACH ROW WHEN (NEW.status <> 'PENDING' AND NEW.job_kwargs IS NOT NULL)
            EXECUTE FUNCTION tasks_drop_job_kwargs()
        """
    ),
)


# ----------------------------------------------------------------
# TASK EVENTS (.github#5)
# ----------------------------------------------------------------
# Progress, log and terminal events of a task, appended by the worker
# (and by the API for cancel / worker_lost). The row id is the SSE event
# id. A trigger (migration 5e1f0c2a9b7d) sends ``NOTIFY task_events``.
class TaskEvent(Base):
    __tablename__ = "task_events"

    id = Column(BigInteger, Identity(always=False), primary_key=True)
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tasks.taskId", ondelete="CASCADE"),
        nullable=False,
    )
    type = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (Index("ix_task_events_task", "task_id", "id"),)


# ----------------------------------------------------------------
# USERTODEPLOYMENT MODEL
# ----------------------------------------------------------------
class UserToDeployment(Base):
    __tablename__ = "user_to_deployments"

    userToDeploymentId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    user = relationship("User", back_populates="user_to_deployments")
    deployment = relationship("Deployment", back_populates="user_to_deployments")


# ----------------------------------------------------------------
# TEAM MODEL
# ----------------------------------------------------------------
class Team(Base):
    __tablename__ = "teams"

    teamId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    deploymentId = Column(
        UUID(as_uuid=True),
        ForeignKey("deployments.deploymentId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    deployment = relationship("Deployment", back_populates="teams")
    user_to_teams = relationship(
        "UserToTeam",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


# ----------------------------------------------------------------
# USERTOTEAM MODEL
# ----------------------------------------------------------------
class UserToTeam(Base):
    __tablename__ = "user_to_teams"

    userToTeamId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    teamId = Column(
        UUID(as_uuid=True),
        ForeignKey("teams.teamId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Relationships
    user = relationship("User", back_populates="user_to_teams")
    team = relationship("Team", back_populates="user_to_teams")


# ----------------------------------------------------------------
# APP VERSION APPROVAL MODEL
# ----------------------------------------------------------------
class AppVersionApproval(Base):
    __tablename__ = "app_version_approvals"

    approvalId = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    appId = Column(
        UUID(as_uuid=True),
        ForeignKey("apps.appId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_tag = Column(String, nullable=False)
    # The commit the tag pointed to at submission. The review covers this
    # commit only: once the tag points elsewhere the version counts as
    # unapproved until it is submitted and reviewed again (.github#7 G).
    # NULL for approvals from before the pinning; they never count.
    commit_sha = Column(String(40), nullable=True)
    status = Column(
        Enum(AppVersionApprovalStatus),
        nullable=False,
        default=AppVersionApprovalStatus.PENDING,
    )
    diff_url = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    reviewed_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    # What the review covered besides the commit (.github#4): how the version
    # runs and the SHA-256 of its appstore.yaml and the image
    # digests it names (JSON list). A deploy refuses a spec that differs.
    runtime = Column(String(16), nullable=False, default=RUNTIME_K8S, server_default=RUNTIME_K8S)
    spec_sha256 = Column(String(64), nullable=True)
    image_digests = Column(Text, nullable=True)

    __table_args__ = (UniqueConstraint("appId", "version_tag", name="uq_app_version_approval"),)

    # Relationships
    app = relationship("App", back_populates="version_approvals")
    reviewer = relationship("User", foreign_keys=[reviewed_by])


# ----------------------------------------------------------------
# LTI 1.3 LAUNCH STATE
# ----------------------------------------------------------------
# The state/nonce pair of an LTI login in flight (``routers/lti13``):
# written by ``/lti13/login``, consumed exactly once by ``/lti13/launch``.
# In the database rather than in process memory, because the two requests
# of one launch can reach different API processes or replicas.
class LtiNonce(Base):
    __tablename__ = "lti_nonces"

    state = Column(String(128), primary_key=True)
    nonce = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (Index("ix_lti_nonces_created_at", "created_at"),)


class LtiLoginCode(Base):
    """A one-time code of an LTI launch, redeemed by the frontend for a session.

    The launch hands the browser the code (in the URL fragment) and sets a
    cookie with a random binding; only a request with both gets the session
    token, so a link with somebody else's code signs nobody in (review S19).
    Only hashes are stored. Redeemed once, within ``LTI_CODE_TTL``.
    """

    __tablename__ = "lti_login_codes"

    code_hash = Column(String(64), primary_key=True)
    binding_hash = Column(String(64), nullable=False)
    userId = Column(
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    __table_args__ = (Index("ix_lti_login_codes_created_at", "created_at"),)


# ----------------------------------------------------------------
# COURSE TEACHER MODEL
# ----------------------------------------------------------------
# Many-to-many join between courses and users. A row ``(course_id,
# user_id)`` declares that ``user`` is one of the teachers responsible
# for ``course``. Backs the per-course "course-teacher" capability.
# Both FKs together form the composite primary key.
class CourseTeacher(Base):
    __tablename__ = "course_teachers"

    courseId = Column(
        "course_id",
        UUID(as_uuid=True),
        ForeignKey("courses.courseId", ondelete="CASCADE"),
        primary_key=True,
    )
    userId = Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.userId", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )

    # Relationships
    course = relationship("Course", back_populates="course_teachers")
    user = relationship("User", back_populates="course_teacher_links")
