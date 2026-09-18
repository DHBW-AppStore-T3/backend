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
  `git_service.py` (klont App-Repos), `celery_event_listener.py`
  (verarbeitet Worker-Events, löst Mail-Benachrichtigungen aus),
  `deployment_notifier.py`, `deployment_pubsub.py` (Redis Pub/Sub →
  SSE ans Frontend), `deployment_status.py`, `reconciler.py`,
  `tf_state_parser.py` (liest Terraform-State aus dem Worker),
  `openstack_client.py`/`openstack_validator.py`,
  `clouds_yaml_parser.py`, `email_service.py`, `lifecycle.py`.
- **`models.py`** — SQLAlchemy-Modelle: `User`, `Course`, `App`,
  `Deployment`, `Task`, `Team`, `UserOpenStackCredential`,
  `AppVersionApproval`, plus Join-Tabellen (`UserToDeployment`,
  `UserToTeam`, `CourseTeacher`).
- **`schemas.py`** — Pydantic-Schemas, getrennt von den ORM-Modellen.

## Request-Flow (typischer Fall: App deployen)

1. Frontend → `POST /deployments` (Router `deployments.py`)
2. Backend legt `Deployment`+`Task`-Zeilen an, published einen Celery-Task
3. Worker (separates Repo) führt Terraform/Packer aus, meldet Fortschritt
4. `celery_event_listener.py` empfängt das Ergebnis, aktualisiert die DB,
   pusht über `deployment_pubsub.py` (Redis) an offene SSE-Verbindungen
5. Frontend erhält den Status live über Server-Sent-Events

Siehe `api-contracts.md` für die Endpunkte, die frontend/worker
tatsächlich konsumieren, `database.md` für das Schema im Detail,
`auth.md` für den Keycloak-Teil.
