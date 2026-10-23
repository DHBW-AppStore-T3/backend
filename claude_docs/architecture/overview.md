# Backend — Überblick

FastAPI-App (`app/main.py`), Poetry-basiert, Python 3.11+. Läuft in
Prod als `backend-prod` hinter nginx unter `/api/`.

## Module (unter `app/`)

- **`routers/`** — ein Router pro Ressource: `apps.py` (App-Katalog,
  größte Datei im Projekt), `deployments.py` (Deploy-Lebenszyklus,
  ebenfalls sehr groß), `courses.py`, `teams.py`, `users.py`,
  `openstack_credentials.py`, `openstack_resources.py`, `quotas.py`,
  `admin_apps.py`, `dashboard.py`, `tasks.py`, `auth_keycloak.py`.
- **`services/`** — Geschäftslogik getrennt von den Routern:
  `git_service.py` (klont App-Repos), `task_service.py` (Task-Zeile
  anlegen, an die Postgres-Queue senden, geparkte Destroys freigeben),
  `task_events.py` (eine LISTEN-Verbindung je Prozess, Backfill für die
  SSE), `task_finalizer.py` (Folgearbeit genau einmal: Soft-Delete nach
  Destroy, Zugangs-Mails), `reconciler.py` (tote Worker → `worker_lost`,
  verlorene Dispatches, geparkte Nachrichten), `task_results.py`
  (Fernet-versiegelte Outputs/State lesen), `deployment_notifier.py`,
  `deployment_status.py`, `tf_state_parser.py`,
  `openstack_client.py`/`openstack_validator.py`,
  `clouds_yaml_parser.py`, `email_service.py`, `lifecycle.py`.
- **`pgq.py` / `task_contract.py`** — Kombu-Transport auf Postgres und
  der Event-/Ergebnis-Vertrag mit dem Worker; identisch im Worker-Repo
  (.github#5).
- **`models.py`** — SQLAlchemy-Modelle: `User`, `Course`, `App`,
  `Deployment`, `Task`, `Team`, `UserOpenStackCredential`,
  `AppVersionApproval`, plus Join-Tabellen (`UserToDeployment`,
  `UserToTeam`, `CourseTeacher`).
- **`schemas.py`** — Pydantic-Schemas, getrennt von den ORM-Modellen.

## Request-Flow (typischer Fall: App deployen)

1. Frontend → `POST /deployments` (Router `deployments.py`)
2. Backend legt `Deployment`+`Task`-Zeilen an und sendet die Celery-Task
   nach dem Commit in die Tabelle `celery_queue` (Task-ID = `taskId`)
3. Worker (separates Repo) holt sie per `SKIP LOCKED`, führt Terraform/Packer
   aus und schreibt Fortschritt/Logs in `task_events`, am Ende Status,
   Transkript und versiegelte Ergebnisse in die Task-Zeile
4. `NOTIFY task_events` weckt die SSE-Streams jedes API-Prozesses; der
   Finalizer erledigt die Folgearbeit genau einmal
5. Frontend erhält den Status live über Server-Sent-Events (mit Event-IDs)

Siehe `api-contracts.md` für die Endpunkte, die frontend/worker
tatsächlich konsumieren, `database.md` für das Schema im Detail,
`auth.md` für den Keycloak-Teil.
