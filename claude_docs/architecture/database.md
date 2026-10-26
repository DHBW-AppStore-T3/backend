# Datenbank & Migrationen

PostgreSQL 16, SQLAlchemy 2.0, Alembic. 28 Revisionen in `alembic/versions/`, Head `c5e7a9b1d3f5` (`alembic heads` zeigt genau eine Zeile).

## Tabellen (`app/models.py`)

| Tabelle | Inhalt |
|---|---|
| `users` | Konto; `role` = höchste aus `keycloak_role`, `lti_role`, `granted_role`; `lti_issuer`/`lti_subject` |
| `courses`, `course_teachers` | Kurse (LTI-Kontext), Lehrende |
| `teams`, `user_to_teams` | Teams eines Deployments |
| `apps` | Katalog; `is_private`, `hidden_by_admin`, `recommended`, `deploy_version`, `deleted_at` (Soft-Delete), Logo als Bytes |
| `app_version_approvals` | Freigabe je `(appId, version_tag)` mit `commit_sha`, `status` (pending/approved/rejected), Notizen, Prüfer |
| `deployments` | `releaseTag`, `commit_sha`, `runtime`, `k8s_namespace` (`dep-<8 hex>`, eindeutig), `k8s_workloads`, `userInputVar`, `deleted_at` |
| `user_to_deployments` | Zugang je Mitglied |
| `tasks` | Auftrag und einzige Wahrheit über seinen Zustand: Typ (deploy, update, destroy, pause, resume, redeploy), Status (pending, running, success, failed, cancelled), `logs`, `outputs_enc` (Fernet über Outputs und State), `claimed_by`, `lease_until`, `cancel_requested_at`, `finalized_at`, `current_phase`, `progress_pct`; höchstens ein PENDING/RUNNING-Task je Deployment (`uq_tasks_active_per_deployment`) |
| `celery_queue` | Queue des Transports `pgq`; `after_task` parkt einen Destroy hinter einem abgebrochenen Job |
| `task_events` | Live-Events; Trigger `task_events_notify` sendet `NOTIFY task_events`; 7 Tage nach Task-Ende gelöscht |
| `lti_nonces`, `lti_login_codes` | LTI-Start |

Die Migration `c5e7a9b1d3f5` hat die Tabellen und Spalten des OpenStack-VM-Pfads entfernt (`terraform_states`, `user_openstack_credentials`, `deployments.os_project_id`, `tasks.state_token_hash`, View `worker_live_deployments`). Alte `runtime`-Werte bleiben in bestehenden Zeilen stehen; `tasks.tf_state`/`outputs` behalten die Historie beendeter Tasks.

Soft-Delete: Apps und Deployments tragen `deleted_at`; Standardabfragen blenden sie aus.

## Rolle `appstore_worker`

Die Worker verbinden sich als `appstore_worker`: Rechte nur auf `celery_queue`, INSERT in `task_events`, die Ergebnis-Spalten von `tasks`. Kein Zugriff auf `users`, `apps` oder Zugangsdaten.
Das Login setzt `python -m app.worker_db_role` (liest `WORKER_DB_PASSWORD`); lokal erledigt das `make migrate-dev`. In Kubernetes ist die Rolle eine CloudNativePG-Managed-Role, das Kommando entfällt.

## Migrationen

- Lokal: `make migrate-dev`, `make migration-create MSG="…"` (aus `deployment/`).
- Kubernetes: Helm-Hook vor jedem Sync (`migrate`-Job, Datenbank-Besitzer).
- Dateinamen `YYYY_MM_DD_HHMM-<revision>_<name>.py`. Neue Revision hängt an den einzigen Head; bei zwei Heads setzt der später gemergte PR `down_revision` neu.
- Tests bauen das Schema mit `create_all`; `tests/test_migrations.py` prüft die echte Kette auf einer Wegwerf-Datenbank (`upgrade head`, `alembic check`, Trigger, Rechte der Rolle, Down/Up).

## Betriebsbefehle

`python -m app.<name>`: `worker_db_role`, `k8s_orphans` (Pod-Namespaces ohne lebendes Deployment), `seal_legacy_results` (Klartext-Ergebnisse alter Tasks versiegeln), `backfill_approval_commits` (Commit älterer Freigaben nachtragen), `cli seed` (Demo-Daten).
