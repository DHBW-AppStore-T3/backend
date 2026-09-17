# Graph Report - backend  (2026-09-15)

## Corpus Check
- 133 files · ~98,246 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 2362 nodes · 6269 edges · 125 communities (99 shown, 7 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 778 edges (avg confidence: 0.93)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Community 0
- Community 1
- Community 2
- Community 3
- Community 4
- Community 5
- Community 6
- Community 7
- Community 8
- Community 9
- Community 10
- Community 11
- Community 12
- Community 13
- Community 14
- Community 15
- Community 16
- Community 17
- Community 18
- Community 19
- Community 20
- Community 21
- Community 22
- Community 23
- Community 24
- Community 25
- Community 26
- Community 27
- Community 28
- Community 29
- Community 30
- Community 31
- Community 32
- Community 33
- Community 34
- Community 35
- Community 36
- Community 37
- Community 38
- Community 39
- Community 40
- Community 41
- Community 42
- Community 43
- Community 44
- Community 45
- Community 46
- Community 47
- Community 48
- Community 49
- Community 50
- Community 51
- Community 52
- Community 53
- Community 54
- Community 55
- Community 56
- Community 57
- Community 58
- Community 59
- Community 60
- Community 61
- Community 62
- Community 63
- Community 64
- Community 65
- Community 66
- Community 67
- Community 68
- Community 69
- Community 70
- Community 71
- Community 72
- Community 73
- Community 74
- Community 75
- Community 76
- Community 77
- Community 78
- Community 79
- Community 80
- Community 81
- Community 82
- Community 83
- Community 84
- Community 85
- Community 86
- Community 87
- Community 88
- Community 89
- Community 90
- Community 91
- Community 92
- Community 93
- Community 94
- Community 95
- Community 96
- Community 97
- Community 115
- Community 116
- Community 117
- Community 118
- Community 119
- Community 120
- Community 121
- Community 122

## God Nodes (most connected - your core abstractions)
1. `User` - 212 edges
2. `UserRole` - 136 edges
3. `TaskStatus` - 63 edges
4. `Deployment` - 58 edges
5. `TaskType` - 57 edges
6. `App` - 57 edges
7. `Team` - 46 edges
8. `OpenStackAuthType` - 44 edges
9. `create_app_in_db()` - 43 edges
10. `Task` - 39 edges

## Surprising Connections (you probably didn't know these)
- `lifecycle Service (Deployment Status Transitions)` --semantically_similar_to--> `App Deployment Request Flow`  [INFERRED] [semantically similar]
  README.md → claude_docs/architecture/overview.md
- `python -m Console-Script Shebang Fix` --conceptually_related_to--> `Backend CI/CD Pipeline`  [INFERRED]
  claude_docs/debugging/common-errors.md → .github/workflows/ci.yml
- `locks (Postgres Advisory Locks Per-User Serialization)` --semantically_similar_to--> `Three Separate Postgres Instances (App/TF-State/Keycloak)`  [INFERRED] [semantically similar]
  README.md → claude_docs/debugging/local-setup-gotchas.md
- `mock_admin()` --uses--> `UserRole`  [INFERRED]
  tests/conftest.py → app/models.py
- `mock_student()` --uses--> `UserRole`  [INFERRED]
  tests/conftest.py → app/models.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Celery Task Status Propagation Pipeline** — readme_task_service, readme_celery_event_listener_service, readme_reconciler_service, readme_deployment_pubsub_service, claude_docs_architecture_database_task_model [INFERRED 0.85]
- **CI Security Gate Pattern (pip-audit + Trivy)** — github_workflows_ci_security_job, github_workflows_ci_pysec_2026_1325_ignore, github_workflows_ci_image_scan_job, claude_docs_debugging_local_setup_gotchas_security_check_dormant_finding [INFERRED 0.85]
- **Deployment Completion Notification Flow** — readme_deployment_notifier_service, readme_email_service, templates_email_owner_summary_html, templates_email_user_invite_html [INFERRED 0.85]

## Communities (125 total, 7 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (61): _access_for_user(), _display_name(), _find_account_for_user(), get_user_access(), _normalise_account_key(), notify_deployment_succeeded(), _output_value(), Any (+53 more)

### Community 1 - "Community 1"
Cohesion: 0.10
Nodes (58): App, User, can_approve_app_version(), can_change_user_role(), can_delete_app(), can_edit_app(), can_edit_course(), can_list_all_apps() (+50 more)

### Community 2 - "Community 2"
Cohesion: 0.08
Nodes (56): create_app_in_db(), _approve_version(), _as(), integration, Tests for the admin emergency deactivation endpoint. Endpoint under test: ``PUT…, Create an APPROVED version record so the app would normally be visible to non-…, Swap the active ``get_current_user_keycloak`` override. Workaround für die…, test_admin_can_deactivate_app_hides_from_students() (+48 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (55): _commit_and_dispatch(), create_deployment(), delete_deployment(), _dispatch_destroy(), _dispatch_lifecycle_task(), download_deployment_file(), _fetch_dispatch_envelope(), _file_var_metadata_only() (+47 more)

### Community 4 - "Community 4"
Cohesion: 0.08
Nodes (53): MarkerError, _parse_marker(), Raised when an ``@openstack`` marker is syntactically or semantically invalid.…, Raise ``MarkerError`` for the malformed-marker shapes a plain regex match would…, Parse the ``@openstack:<type>[:<mode>][:<multi>][:<var_scope>]`` marker from…, Verify a ``@openstack:file:<scope>``-marked variable has the HCL type the…, Verify a non-file variable marked with ``var_scope = team|user`` has a map-…, _reject_malformed_markers() (+45 more)

### Community 5 - "Community 5"
Cohesion: 0.09
Nodes (53): Create or update the user's credential row. `validation_result` comes from…, upsert(), _ac_payload(), _make_app(), _make_user(), integration, Integration-tests for two CRUD-Module mit niedriger Coverage. Deckt die…, Erneutes submit auf eine PENDING-Zeile gibt 409. (+45 more)

### Community 6 - "Community 6"
Cohesion: 0.06
Nodes (34): GitService, Any, Session, Git service for repository management and release information retrieval., Fetch all tags from GitHub API., Fetch releases from GitHub API., Fetch all tags from GitLab API., Fetch releases from GitLab API. (+26 more)

### Community 7 - "Community 7"
Cohesion: 0.10
Nodes (49): add_users_to_course(), create_course(), delete_course(), get_course(), get_course_members(), get_courses(), Session, UUID (+41 more)

### Community 8 - "Community 8"
Cohesion: 0.08
Nodes (41): OpenStackAuthType, OpenStackCredentialUpsert, Body of PUT /me/openstack-credentials. `identifier` is either a username…, _bad(), parse(), HTTPException, Parse a raw `clouds.yaml` blob into an `OpenStackCredentialUpsert`. Used by the…, _build_connect_kwargs() (+33 more)

### Community 9 - "Community 9"
Cohesion: 0.13
Nodes (44): CourseTeacher, TestClient, _assign_teacher(), _make_course(), _make_user(), _override_session(), integration, Phase A2 — Tests for ``app.routers.courses``. Covers the full course-management… (+36 more)

### Community 10 - "Community 10"
Cohesion: 0.08
Nodes (42): admin(), _app(), _course(), db_mock(), _deployment(), fixture, SimpleNamespace, UUID (+34 more)

### Community 11 - "Community 11"
Cohesion: 0.12
Nodes (39): _add_member(), _make_deployment(), _make_team(), _override(), integration, Team, Phase A3 — Teams-API Tests. Tests für ``backend/app/routers/teams.py``. Die…, Setze Auth- und DB-Override für ``user`` und liefere TestClient. (+31 more)

### Community 12 - "Community 12"
Cohesion: 0.11
Nodes (39): bulk_get_task_summary(), count_active_user_deployments(), create_deployment(), create_user_to_deployments(), get_deployment(), get_deployment_created_at(), get_deployment_outputs(), get_deployment_status() (+31 more)

### Community 13 - "Community 13"
Cohesion: 0.09
Nodes (39): AppVersionApprovalSubmit, CourseBase, CourseMembersUpdate, CourseResponse, CourseWithUsers, DeploymentBase, DeploymentOutputs, DeploymentResourceListResponse (+31 more)

### Community 14 - "Community 14"
Cohesion: 0.09
Nodes (38): _addresses_from(), NetworkAddress, Flatten the ``addresses`` dict into one row per (network, IP). OpenStack's…, One entry per NIC × IP. Networks with multiple addresses (fixed + floating)…, _translate_power_state(), parametrize, unit, Unit-Tests fuer die reinen Adapter-Funktionen in… (+30 more)

### Community 15 - "Community 15"
Cohesion: 0.09
Nodes (38): _make_course(), _make_user(), integration, Integration tests for crud.tasks, crud.users and crud.apps. Covers branch…, get_task ohne Treffer liefert None., update_task liefert None, wenn der Task nicht existiert., create_user erzeugt eine User-Row mit ID., get_user trifft die Row und liefert sonst None. (+30 more)

### Community 16 - "Community 16"
Cohesion: 0.13
Nodes (37): list_availability_zones(), fetch(), list_flavors(), fetch(), list_floating_ip_pools(), fetch(), list_images(), fetch() (+29 more)

### Community 17 - "Community 17"
Cohesion: 0.13
Nodes (37): TeamCreate, _make_app(), _make_course(), _make_deployment(), _make_user(), integration, Integration tests for ``app.crud.teams`` und ``app.crud.courses``. Beide CRUD-…, ``userIds=[]`` darf keine Membership-Rows erzeugen. (+29 more)

### Community 18 - "Community 18"
Cohesion: 0.10
Nodes (35): build_image_data_url(), parse_image_data_url(), Helpers for the App-image data-URL ↔ bytes round-trip. The API exposes…, Decode a data-URL into ``(bytes, mime)``. Returns ``(None, None)`` for ``None``…, Build a data-URL for the API response. Returns ``None`` if either side is…, unit, Unit tests für ``app.utils.app_image`` (data-URL ↔ bytes Round-trip)., Nicht-image-Mimes matchen die Regex nicht und ergeben 422. (+27 more)

### Community 19 - "Community 19"
Cohesion: 0.14
Nodes (33): approve(), _get_approval_or_404(), get_approvals_for_app(), get_pending_approvals(), has_any_approved_version(), has_approved_version(), Session, UUID (+25 more)

### Community 20 - "Community 20"
Cohesion: 0.20
Nodes (30): UserRole, ensure_deployment_owner_view(), get_current_active_user(), is_deployment_owner_view(), True if ``user`` should see the *owner view* of ``deployment``. The owner view…, Raise 403 unless ``user`` has the owner view of ``deployment``. Use on…, Get current active user, FastAPI dependency factory enforcing a role allow-list. Returns a dependency… (+22 more)

### Community 21 - "Community 21"
Cohesion: 0.12
Nodes (29): AppVariableResponse, create_app(), get_app(), get_app_variables(), list_apps(), list_version_approvals(), _MarkerErrorPayload, BaseModel (+21 more)

### Community 22 - "Community 22"
Cohesion: 0.14
Nodes (27): _attach_files_to_user_input(), Validate and merge wizard-uploaded files into ``userInputVar``. The wizard…, Enforce that variables marked with ``varScope = team|user`` arrive as a map…, _validate_scoped_user_input(), DeploymentFileUpload, Single file uploaded by the teacher in the deploy wizard. Persisted verbatim…, Team, _payload() (+19 more)

### Community 23 - "Community 23"
Cohesion: 0.11
Nodes (28): _build_cipher(), decrypt_b64(), encrypt(), encrypt_b64(), Symmetric encryption for at-rest credentials and Celery-envelope payloads. The…, Encrypt a string. Returns Fernet ciphertext as bytes (store as BYTEA)., Encrypt and base64-encode for JSON-safe transport (Celery args)., Inverse of `encrypt_b64`. Raises InvalidToken on bad input. (+20 more)

### Community 24 - "Community 24"
Cohesion: 0.11
Nodes (29): _make_user(), integration, Phase B1 — Tests für die ``/users``-Endpoints. Deckt die wichtigsten Pfade aus…, Teacher zählt als Staff — RBAC erlaubt den Listen-Zugriff., Student darf die Liste *nicht* sehen — 403 aus ``require_staff``., Keycloak-Suche liefert die gemockten KC-User als Liste zurück. Die Route ``GET…, Student darf das eigene Profil per ``GET /users/{id}`` lesen., Student darf *keinen* anderen User abrufen. (+21 more)

### Community 25 - "Community 25"
Cohesion: 0.12
Nodes (27): build_resource_detail(), build_resource_views(), DeploymentResourceView, _enrich_instances_stage1(), _fetch_ports(), _fetch_sg_summaries(), _fetch_volumes(), NetworkPort (+19 more)

### Community 26 - "Community 26"
Cohesion: 0.11
Nodes (23): existing_app(), fixture, integration, A STUDENT who is not the owner must not be able to update the app., Phase 2 — Bug #2: ein TEACHER, der nicht Owner ist, darf fremde Apps NICHT…, Phase 2 — Bug #2: ein TEACHER darf fremde Apps NICHT löschen., Admin behält den blanket-Delete auf fremde Apps., Phase 2 — Bug #1: nur Admin darf ``role`` auf ``PUT /users/{id}`` setzen. Ein… (+15 more)

### Community 27 - "Community 27"
Cohesion: 0.18
Nodes (26): _common_metadata(), delete(), get_decrypted_for_backend(), get_dispatch_envelope(), get_for_user(), NoCredentialError, Exception, Session (+18 more)

### Community 28 - "Community 28"
Cohesion: 0.15
Nodes (24): derive_status(), Synthesize the effective deployment status from the latest task's ``(status,…, _make_app(), _make_deployment(), _make_task(), integration, parametrize, unit (+16 more)

### Community 29 - "Community 29"
Cohesion: 0.18
Nodes (25): _ensure_app(), _ensure_user_credentials(), _make_server_mock(), patched_celery_send(), patched_user_connection(), fixture, integration, API-level tests for the per-deployment resource endpoints. Covers: * GET… (+17 more)

### Community 30 - "Community 30"
Cohesion: 0.13
Nodes (23): Deployment, UserToDeployment, DashboardStatsResponse, get_dashboard_stats(), BaseModel, get, Session, Aggregate counts for the dashboard KPI strip. Cheap DB-only aggregates —… (+15 more)

### Community 31 - "Community 31"
Cohesion: 0.13
Nodes (24): _build_connect_kwargs(), cached_list(), invalidate_user(), _make_key(), Any, Session, UUID, Shared OpenStack client layer for FastAPI endpoints. Three responsibilities: 1.… (+16 more)

### Community 32 - "Community 32"
Cohesion: 0.15
Nodes (24): create_app(), get_app(), get_apps(), get_visible_apps(), Session, UUID, Persist the image bytes + mime atomically. Both args ``None`` clears the image.…, Mark an app as deleted without removing the row. The row stays so existing… (+16 more)

### Community 33 - "Community 33"
Cohesion: 0.18
Nodes (24): TaskType, _make_deployment_with_creds(), integration, parametrize, Integration tests for the pause / resume HTTP endpoints. These tests run…, A successful deploy is not a valid resume target — only paused is., Paused deployments still hold OpenStack resources — Destroy works., DELETE must 409 while *any* lifecycle task is in flight, regardless of which… (+16 more)

### Community 34 - "Community 34"
Cohesion: 0.15
Nodes (23): parse_tf_state(), Parse a ``terraform state pull`` JSON blob into a typed list. Accepts the raw…, parametrize, unit, Unit tests for ``app/services/tf_state_parser.py``. Pure-function coverage: no…, ``terraform state list`` prints quoted for_each keys — our addresses must match…, ``count.index`` produces integer ``index_key`` — address should use bare…, ``random_password`` and other irrelevant types must NOT leak into the resource… (+15 more)

### Community 35 - "Community 35"
Cohesion: 0.14
Nodes (23): fixture, integration, Integration tests for POST…, Owner (creator) hits the endpoint for some member — notifier is called with the…, A plain team member (non-owner-view) may resend the mail for themself. The…, Admins get the owner view of every deployment, so they may resend the mail for…, A student who has no relation to the deployment (not in any of its teams, no…, When the notifier returns ``False`` (template render OK but SMTP rejected the… (+15 more)

### Community 36 - "Community 36"
Cohesion: 0.21
Nodes (22): _add_team_members(), add_user_to_team(), create_team(), create_teams_for_deployment(), delete_team(), get_team(), get_teams(), Session (+14 more)

### Community 37 - "Community 37"
Cohesion: 0.11
Nodes (22): Pick a human-readable headline + failure_kind for known Celery infrastructure…, _translate_celery_infra_exception(), parametrize, unit, Pure-function tests for the celery infrastructure-exception translator in…, The most common operator-facing problem: backend and worker are out of sync.…, The translator is best-effort. Unknown exception classes should let the caller…, test_empty_input_returns_none() (+14 more)

### Community 38 - "Community 38"
Cohesion: 0.16
Nodes (21): allowed_actions(), DeploymentAction, ensure_action_allowed(), str, Deployment lifecycle gating — single source of truth for which actions are…, Return the set of actions allowed for the given deployment. ``deployment`` can…, Raise ``HTTPException(409)`` if ``action`` isn't allowed right now. Produces a…, Lifecycle actions a user can request. (+13 more)

### Community 39 - "Community 39"
Cohesion: 0.10
Nodes (22): _check_multi_type_conflict(), _closest_match(), _collection_check_type(), _forbid_packer_team_user_scope(), _parse_file_marker(), _parse_marker_mode(), _parse_marker_multi(), _parse_resource_marker() (+14 more)

### Community 40 - "Community 40"
Cohesion: 0.15
Nodes (19): admin(), db_mock(), fixture, parametrize, Unit tests for :mod:`app.utils.capabilities`. Every capability function gets…, student(), teacher(), test_can_approve_app_version() (+11 more)

### Community 41 - "Community 41"
Cohesion: 0.16
Nodes (19): acquire_deployment_xact_lock(), acquire_user_xact_lock(), Session, UUID, Per-user serialization via Postgres advisory locks. Used to make credential…, Block until this transaction holds the per-user advisory lock. `hashtext()`…, Per-deployment advisory lock — serialises lifecycle dispatch. Used by every…, integration (+11 more)

### Community 42 - "Community 42"
Cohesion: 0.17
Nodes (20): delete_task(), get_task(), get_tasks(), Session, UUID, Get tasks with optional filters, Update task information, update_task() (+12 more)

### Community 43 - "Community 43"
Cohesion: 0.17
Nodes (20): add_user_to_team(), create_team(), delete_team(), get_team(), list_teams(), get, post, put (+12 more)

### Community 44 - "Community 44"
Cohesion: 0.16
Nodes (18): admin_client(), client(), override_get_current_user(), _make_client(), override_get_current_user(), override_get_db(), mock_admin(), mock_student() (+10 more)

### Community 45 - "Community 45"
Cohesion: 0.19
Nodes (17): Submit a version for admin review. Raises 409 if the version already has a…, submit_version(), _make_other_teacher(), _override_user(), integration, Phase B4 — Tests für Withdraw und Approval-History. Coverage: - Owner kann eine…, Phase-2-Bug-#2-fix: ``GET /apps/{id}/versions`` ist owner-/admin- only,…, Bug #2 Regression: ein Teacher darf eine fremde App nicht withdrawen — nur… (+9 more)

### Community 46 - "Community 46"
Cohesion: 0.20
Nodes (19): approve_version(), deactivate_app(), list_pending_versions(), get, post, put, Session, UUID (+11 more)

### Community 47 - "Community 47"
Cohesion: 0.18
Nodes (18): _discover_packer_templates(), _PackerTemplate, PackerTemplateDiscoveryError, One Packer template discovered under ``<repo>/packer``. ``variables_path`` may…, Raised when the Packer directory has a layout the platform can't reconcile…, Walk ``<repo_path>/packer`` and return the list of templates the worker will…, model_validator, Unit tests for ``_discover_packer_templates``. The discovery helper picks one… (+10 more)

### Community 48 - "Community 48"
Cohesion: 0.21
Nodes (19): _assert_unlocked(), delete_my_credentials(), get_my_credentials(), _lock_state(), get, post, put, Session (+11 more)

### Community 49 - "Community 49"
Cohesion: 0.16
Nodes (19): format_logs(), _get_icon(), Get the leading ASCII marker for a log level. Uses a fixed four-character tag…, Format logs from structured format to readable text, Unit tests for the pure log helpers in celery_event_listener. Diese Tests…, Lange, mehrzeilige Messages mit > 20 Zeilen werden gefiltert., Keine Liste -> str() Konvertierung., Unbekanntes Level liefert den Default-Marker. (+11 more)

### Community 50 - "Community 50"
Cohesion: 0.18
Nodes (18): _client_for(), integration, Tests for ``GET /dashboard/stats`` — Phase B2. Covers the role-branched…, Return a TestClient whose auth dependency yields ``user``. Caller is…, Admin gets the platform-wide app view (5 apps, deleted excluded), plus the…, Teacher must see: own apps (A1, A2, A3 — regardless of approval state or…, Teacher deployments counter mirrors ``GET /deployments`` — owner only, no…, Student visibility (mirrors crud_apps.get_visible_apps and… (+10 more)

### Community 51 - "Community 51"
Cohesion: 0.14
Nodes (11): AbstractEventLoop, DeploymentPubSub, Any, In-process pub/sub bridge between the Celery event listener and the SSE…, Push ``event`` to every subscriber for ``deployment_id``. Threadsafe. If the…, Push onto the queue; on full queues drop oldest + signal overflow. Runs inside…, One-process, in-memory fan-out keyed by ``deployment_id``. Construct once at…, Bind the FastAPI event loop. Called once during lifespan startup. ``publish``… (+3 more)

### Community 52 - "Community 52"
Cohesion: 0.18
Nodes (18): get_me(), get_user(), get_user_statistics(), list_users(), get, put, Session, UUID (+10 more)

### Community 53 - "Community 53"
Cohesion: 0.14
Nodes (18): _build_entry(), _coerce_state(), _extract_team(), _format_address(), _pick_display_name(), Generic Terraform-state parser for the deployment status pipeline. Reads the…, Best-effort: return a state dict or None. Invalid JSON is logged at WARN and…, Materialise one resource INSTANCE into a ``TfResource``. Returns None when the… (+10 more)

### Community 54 - "Community 54"
Cohesion: 0.25
Nodes (18): patch, integration, Phase B7: API-level tests for `/me/openstack-credentials`. Covers the five HTTP…, _seed_credential(), test_delete_my_credentials_idempotent_when_absent(), test_delete_my_credentials_removes_row(), test_get_my_credentials_404_when_absent(), test_get_my_credentials_returns_redacted_view() (+10 more)

### Community 55 - "Community 55"
Cohesion: 0.19
Nodes (18): _outputs_for(), integration, Integration tests for GET /deployments/{id}/my-access. The endpoint lets a team…, A team member retrieves their own credentials (200, one key)., Core security guarantee: the response must NOT contain any other member's…, A student with no relation to the deployment (not in any team) is rejected at…, No outputs yet → 200 with empty maps (clean 'no credentials' state, not a…, The owner uses the same endpoint and gets their own account back (no regression… (+10 more)

### Community 56 - "Community 56"
Cohesion: 0.16
Nodes (7): _deployment(), UUID, Mirrors ``has_deployment_access`` — owner, staff, team, direct., Phase 2: operate is owner-or-admin only. Teachers no longer get a blanket…, TestCanOperateDeployment, TestCanResendAccess, TestCanViewDeploymentMember

### Community 57 - "Community 57"
Cohesion: 0.18
Nodes (17): _is_destroyed_subq(), Correlated EXISTS: deployment has a successful DESTROY task., str, TaskStatus, _handle_task_succeeded(), Build the update payload for a ``task-succeeded`` event. Pulls the full result…, _extract_failure_logs(), _extract_success_payload() (+9 more)

### Community 58 - "Community 58"
Cohesion: 0.15
Nodes (17): _apply_infra_failure(), _apply_structured_failure(), _auto_soft_delete_on_destroy(), _handle_task_failed(), _notify_deploy_succeeded(), _parse_structured_failure(), _publish_lifecycle_transition(), Any (+9 more)

### Community 59 - "Community 59"
Cohesion: 0.18
Nodes (18): is_verbose_line(), Check if line is very verbose (TRACE/DEBUG from tools), unit, Echte User-Ausgaben werden nicht als verbose erkannt., Die Übersetzungstabelle hat (needle, headline, kind) Tripel., test_celery_infra_exceptions_table_well_formed(), test_is_verbose_line_detects_binary_installation_options(), test_is_verbose_line_detects_debug_marker() (+10 more)

### Community 60 - "Community 60"
Cohesion: 0.16
Nodes (17): get_current_user_keycloak(), get_keycloak_client(), _get_realm_public_key_pem(), map_keycloak_roles_to_app_role(), Session, Keycloak Authentication & Authorization Handles token validation and user…, Validate JWT signature against Keycloak public key (fast, no server round-trip)., Priority: admin > teacher > student (+9 more)

### Community 61 - "Community 61"
Cohesion: 0.19
Nodes (15): Pull the latest Keycloak record for ``user`` and reconcile our DB row. Used by…, refresh_user_from_keycloak(), _make_user(), unit, Tests for the just-in-time Keycloak refresh used by the notifier., ``get_user`` doesn't carry roles — the DB role must survive the refresh., Happy path — KC has a newer email than the DB row., KC down → log and return the DB record unchanged. (+7 more)

### Community 62 - "Community 62"
Cohesion: 0.18
Nodes (16): _clear_openstack_cache(), _flavor_mock(), _image_mock(), patched_user_connection(), fixture, integration, API-Level Tests für die OpenStack-Resources-Read-API. Abgedeckt: * GET…, Patcht den Context-Manager ``user_connection`` im Resources-Router. Liefert ein… (+8 more)

### Community 63 - "Community 63"
Cohesion: 0.21
Nodes (17): _ensure_user_credentials(), _make_quota_conn(), integration, skip, API-level tests for ``GET /quotas/overview``. The endpoint is wired to…, Admin role acts as a platform operator and is allowed to look at another user's…, Non-admins must not be able to scope quotas to a different user. The router has…, No auth override is installed → ``get_current_user_keycloak`` runs for real and… (+9 more)

### Community 64 - "Community 64"
Cohesion: 0.23
Nodes (16): create_user(), delete_user(), get_user(), get_user_by_email(), get_user_by_username(), get_users(), Session, UUID (+8 more)

### Community 65 - "Community 65"
Cohesion: 0.16
Nodes (17): _apply_defaults(), _coerce_hcl_default(), _iter_variable_blocks(), _line_number_at(), load_variable_definitions(), _parse_one_variable(), _parse_packer_variables(), _parse_terraform_variables() (+9 more)

### Community 66 - "Community 66"
Cohesion: 0.12
Nodes (17): _one(), _lifecycle_from(), LifecycleStates, Pull the four lifecycle states + fault message from a server. Tries the…, Server lifecycle quad — together they cover every diagnostic case., ACTIVE-Server fuellt status/task/vm/power, fault bleibt None., ERROR + fault als dict liefert ``fault.message``., ERROR + fault als Objekt (SDK-Munch) liefert ``message`` per getattr. (+9 more)

### Community 67 - "Community 67"
Cohesion: 0.31
Nodes (16): _b64(), _ensure_app(), _ensure_user_credentials(), patched_celery(), fixture, integration, Integration tests for the deployment file-upload contract. Covers the round-…, One file over 2 MB → 413, no row created, no celery dispatch. (+8 more)

### Community 68 - "Community 68"
Cohesion: 0.19
Nodes (4): _app(), TestCanEditApp, TestCanViewApp, TestEnsureViewApp

### Community 69 - "Community 69"
Cohesion: 0.24
Nodes (15): _build_connect_kwargs(), ComputeQuotas, _get_openstack_conn_for_user(), get_quota_overview(), NetworkQuotas, BaseModel, get, Session (+7 more)

### Community 70 - "Community 70"
Cohesion: 0.23
Nodes (15): _make_app(), _make_deployment(), _make_task(), integration, Tests for the Tasks-API router — ``app.routers.tasks``. Phase C10: read-only…, Ein STUDENT ohne Team-/Direkt-Zuordnung bekommt 403., Owner liest eine einzelne Task per ID., Unbekannte Task-ID liefert 404. (+7 more)

### Community 71 - "Community 71"
Cohesion: 0.21
Nodes (15): create_task(), _make_app(), _make_deployment(), create_task speichert die übergebenen Felder., get_tasks filtert nach deployment_id, celery_task_id und status., update_task übernimmt ein dict 1:1., update_task akzeptiert auch ein TaskUpdate-Modell mit exclude_unset., delete_task: True beim ersten Mal, False danach. (+7 more)

### Community 72 - "Community 72"
Cohesion: 0.18
Nodes (13): _background_tasks_disabled(), health_check(), lifespan(), get, auth_health(), get, Auth Router - Keycloak Version Serves the public auth health check so operators…, Check if auth service is healthy (+5 more)

### Community 73 - "Community 73"
Cohesion: 0.22
Nodes (13): Try to authorize. Returns (ok, error_message). The error message is short and…, validate(), _appcred_payload(), _password_payload(), Unit tests for :mod:`app.services.openstack_validator`. The tests mock…, A 401 from Keystone maps to ``Invalid credentials``; secret is not echoed., gaierror / TimeoutError surface as a short ``Could not reach auth_url`` message., Password auth: connect() receives v3password kwargs and authorize() is called. (+5 more)

### Community 74 - "Community 74"
Cohesion: 0.17
Nodes (13): _hardware_from(), HardwareSpec, Compact hardware/image footprint for the card., SDK liefert flavor/image als dict => alle Felder werden geholt., flavor als getypter SDK-Objekt-Wert (Munch) wird best-effort geparst., Fallback: kein ``original_name`` => ``name``-Key wird verwendet., Server ohne image => image_id bleibt None., Server ohne flavor/image => alle Felder None, kein Crash. (+5 more)

### Community 75 - "Community 75"
Cohesion: 0.23
Nodes (5): _course(), Phase 3: ``is_course_teacher`` queries the ``course_teachers`` join table. Role…, Phase 3: ``can_edit_course`` is admin OR course-teacher of THIS course. The…, TestCanEditCourse, TestIsCourseTeacher

### Community 76 - "Community 76"
Cohesion: 0.18
Nodes (9): Run migrations in 'offline' mode. This configures the context with just a URL…, Run migrations in 'online' mode. In this scenario we need to create an Engine…, run_migrations_offline(), run_migrations_online(), Config, Settings, get_db(), Database session dependency (+1 more)

### Community 77 - "Community 77"
Cohesion: 0.21
Nodes (10): ActiveTaskExistsError, dispatch_to_celery(), prepare_task_in_tx(), Exception, Session, UUID, Task lifecycle helpers. Two-phase dispatch: 1. `prepare_task_in_tx` — INSERT a…, A PENDING/RUNNING task already exists for this deployment. (+2 more)

### Community 78 - "Community 78"
Cohesion: 0.32
Nodes (12): Poetry as Dependency Source of Truth, Security Check Dormant-Until-2026-09-15 Finding, Backend CI/CD Pipeline, Docker Build Job, Coverage Aggregation Job, Image Scan Job (Trivy), Lint Job (Ruff), Push Job (Registry Push) (+4 more)

### Community 79 - "Community 79"
Cohesion: 0.20
Nodes (11): Alembic Migrations Guide, App Model (Versioned via AppVersionApproval), Course Model, Manual Migration Strategy (No Auto-Migrate on Container Start), Database Schema (PostgreSQL, SQLAlchemy 2.0, Alembic), Team Model, UserOpenStackCredential Model, CREDENTIAL_ENCRYPTION_KEY Missing Failure Mode (+3 more)

### Community 80 - "Community 80"
Cohesion: 0.20
Nodes (10): clean_log_line(), Clean up log line by removing ANSI escape codes and normalizing quotes, ANSI-Farbcodes werden vollständig entfernt., Reiner Text bleibt unverändert., Doppelte Anführungszeichen werden zu einfachen normalisiert., test_clean_log_line_handles_empty_string(), test_clean_log_line_noop_on_plain_text(), test_clean_log_line_normalizes_doubled_quotes() (+2 more)

### Community 81 - "Community 81"
Cohesion: 0.20
Nodes (10): filter_logs(), Filter and clean logs for better readability - Remove very verbose lines…, Verbose Zeilen werden entfernt, übrige bleiben in Reihenfolge., Bei zu vielen Zeilen wird auf Kopf + Schwanz mit Ellipsis gekürzt., test_filter_logs_drops_empty_lines(), test_filter_logs_drops_verbose_lines_keeps_order(), test_filter_logs_handles_empty_string(), test_filter_logs_no_truncation_when_under_max_lines() (+2 more)

### Community 82 - "Community 82"
Cohesion: 0.20
Nodes (10): Backend to Worker Contract (via Celery/RabbitMQ), Terraform State DB Isolation (postgres-tfstate), Deployment Model, Task Model (TaskType/TaskStatus Enums), Three Separate Postgres Instances (App/TF-State/Keycloak), deployments Router (/deployments), locks (Postgres Advisory Locks Per-User Serialization), task_service (Two-Phase Dispatch) (+2 more)

### Community 83 - "Community 83"
Cohesion: 0.25
Nodes (9): get_keycloak_admin(), get_keycloak_users_by_ids(), _project_keycloak_user(), Project a raw Keycloak user record into our simplified dict shape. Includes the…, Search Keycloak users by username/email/name (uses service account)., Resolve a list of Keycloak IDs to simplified user dicts., Get Keycloak Admin client (uses service-account client_credentials grant)., search_keycloak_users() (+1 more)

### Community 84 - "Community 84"
Cohesion: 0.42
Nodes (9): deployment_notifier Service, email_service (SMTP + Jinja2 Templating), auth_type Branching Pattern (password/ssh_key/oauth/none), base.html Email Layout Template, Inline-Style Email Compatibility Pattern, owner_summary.html Deployment Summary Email (HTML), owner_summary.txt Deployment Summary Email (Plaintext), user_invite.html Access Details Email (HTML) (+1 more)

### Community 85 - "Community 85"
Cohesion: 0.25
Nodes (8): _event_name_for(), Live progress + log stream for one deployment as Server-Sent Events. The…, Map Celery event type names onto short SSE event names. Frontend code attaches…, Serialise one SSE frame. SSE format: ``` event: <name>\\n data: <json>\\n \\n…, _sse_frame(), stream_deployment_events(), event_stream(), Request

### Community 86 - "Community 86"
Cohesion: 0.36
Nodes (8): Frontend to Backend API Contract (/api/ prefix), Worker to Backend Results Path (Redis), App Deployment Request Flow, DISABLE_BACKGROUND_TASKS Test Flag, celery_event_listener Service, deployment_pubsub Service (Listener to SSE Bridge), lifecycle Service (Deployment Status Transitions), reconciler Service

### Community 87 - "Community 87"
Cohesion: 0.32
Nodes (8): Backend Overview, Backend README Overview, crud/ Layer (DB Access), deployment Repo (Stack Orchestration), routers/ Layer (HTTP + Auth), services/ Layer (Business Logic), utils/ Layer (Cross-Cutting Helpers), worker Repo (Celery Worker Service)

### Community 88 - "Community 88"
Cohesion: 0.32
Nodes (8): CORS_ORIGINS Merge Conflict, python -m Console-Script Shebang Fix, FastAPI vs Django/Flask Decision, SQLAlchemy 2.0 ORM Choice, 2026-09-15 claude_docs + Graphify Setup Log Entry, Backend Repo CLAUDE.md Notes, HARNESS.md Multi-Repo Harness Concept, .github Repo technologiestack.md

### Community 89 - "Community 89"
Cohesion: 0.29
Nodes (7): Keycloak Integration (RS256 JWT Validation), python-jose / ecdsa Timing Side-Channel Tolerance, Keycloak Token Flow, User Model (UserRole Enum), PYSEC-2026-1325 pip-audit Ignore (ecdsa Timing Side-Channel), auth_keycloak Router (/auth), keycloak_auth util (Token Validation)

### Community 90 - "Community 90"
Cohesion: 0.33
Nodes (6): fixture, Unit-test scoped conftest. The parent ``tests/conftest.py`` declares two…, No-op override of the parent session-scoped schema fixture., No-op override of the parent per-test truncate fixture., _setup_schema(), _truncate_tables()

### Community 91 - "Community 91"
Cohesion: 0.47
Nodes (3): SimpleNamespace, Phase 3: owner-view = owner OR admin OR course-teacher of the deployment-…, TestCanViewDeploymentOwner

### Community 92 - "Community 92"
Cohesion: 0.40
Nodes (5): AppBase, AppCreate, AppResponse, AppWithUser, AppWithVersions

### Community 93 - "Community 93"
Cohesion: 0.40
Nodes (5): _make_approval(), Public + approved App ist für andere Studenten sichtbar., Public, aber ohne approved Approval -> für Andere unsichtbar., test_get_visible_apps_hides_public_unapproved(), test_get_visible_apps_shows_public_approved()

### Community 94 - "Community 94"
Cohesion: 0.40
Nodes (5): patched_celery(), fixture, Replace Celery's ``send_task`` with a stub that returns a fake id. The real…, Most tests in this file don't care about SMTP, but the…, _smtp_enabled_default()

### Community 97 - "Community 97"
Cohesion: 0.67
Nodes (3): unit, test_health_no_auth_required(), test_health_returns_200()

### Community 115 - "Community 115"
Cohesion: 0.67
Nodes (3): post, Cache bust for the calling user. Triggered by a click on the "Refresh" button…, refresh_cache()

### Community 116 - "Community 116"
Cohesion: 0.67
Nodes (3): clouds_yaml_parser Service, openstack_client Service, openstack_validator Service

### Community 117 - "Community 117"
Cohesion: 0.67
Nodes (3): _clear_cache(), fixture, Modul-globaler Cache wird vor und nach jedem Test geleert, damit Tests sich…

## Knowledge Gaps
- **18 isolated node(s):** `Config`, `backend-api`, `deployment Repo (Stack Orchestration)`, `worker Repo (Celery Worker Service)`, `utils/ Layer (Cross-Cutting Helpers)` (+13 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 900 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **7 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `User` connect `Community 1` to `Community 0`, `Community 3`, `Community 5`, `Community 7`, `Community 8`, `Community 9`, `Community 11`, `Community 12`, `Community 15`, `Community 16`, `Community 17`, `Community 20`, `Community 21`, `Community 24`, `Community 25`, `Community 26`, `Community 28`, `Community 29`, `Community 30`, `Community 31`, `Community 32`, `Community 38`, `Community 43`, `Community 44`, `Community 45`, `Community 46`, `Community 48`, `Community 50`, `Community 52`, `Community 60`, `Community 61`, `Community 64`, `Community 67`, `Community 69`, `Community 85`, `Community 115`?**
  _High betweenness centrality (0.232) - this node is a cross-community bridge._
- **Why does `UserRole` connect `Community 20` to `Community 1`, `Community 3`, `Community 5`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 11`, `Community 13`, `Community 15`, `Community 17`, `Community 19`, `Community 21`, `Community 24`, `Community 26`, `Community 28`, `Community 30`, `Community 40`, `Community 44`, `Community 45`, `Community 50`, `Community 52`, `Community 57`, `Community 60`, `Community 61`, `Community 64`, `Community 67`, `Community 91`, `Community 118`?**
  _High betweenness centrality (0.094) - this node is a cross-community bridge._
- **Why does `TaskStatus` connect `Community 57` to `Community 33`, `Community 3`, `Community 35`, `Community 38`, `Community 71`, `Community 8`, `Community 70`, `Community 42`, `Community 12`, `Community 13`, `Community 77`, `Community 15`, `Community 85`, `Community 55`, `Community 58`, `Community 28`, `Community 29`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Are the 163 inferred relationships involving `User` (e.g. with `add_users_to_course()` and `delete_course()`) actually correct?**
  _`User` has 163 INFERRED edges - model-reasoned connections that need verification._
- **Are the 108 inferred relationships involving `UserRole` (e.g. with `get_users()` and `get_app()`) actually correct?**
  _`UserRole` has 108 INFERRED edges - model-reasoned connections that need verification._
- **Are the 45 inferred relationships involving `TaskStatus` (e.g. with `bulk_get_task_summary()` and `derive_status()`) actually correct?**
  _`TaskStatus` has 45 INFERRED edges - model-reasoned connections that need verification._
- **Are the 25 inferred relationships involving `Deployment` (e.g. with `count_active_user_deployments()` and `get_deployment()`) actually correct?**
  _`Deployment` has 25 INFERRED edges - model-reasoned connections that need verification._