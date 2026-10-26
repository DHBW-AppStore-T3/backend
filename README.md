# Backend

FastAPI-API des AppStore. Validiert Keycloak-Tokens, hält alle Daten in PostgreSQL, legt Deployment-Aufträge als Tasks an und reicht sie über die Postgres-Queue `pgq` an die Worker weiter. Live-Status per Server-Sent Events.

Zustand: Branch `dev` (Staging). `main` ist Produktion und hinkt `dev` bis zur nächsten Promotion hinterher.

## Lokal starten

Das Backend läuft nie allein. Der Stack (Postgres, Keycloak, Backend, Worker, Frontend) startet aus dem Repo `deployment`.
Komplette Anleitung ab Null (Terminal, Git, Docker, make): [deployment/docs/dev-setup.md](https://github.com/DHBW-AppStore-T3/deployment/blob/dev/docs/dev-setup.md).

Kurzfassung, wenn Docker, Git und make installiert sind:

```bash
mkdir appstore && cd appstore
for r in deployment backend worker frontend; do git clone -b dev https://github.com/DHBW-AppStore-T3/$r.git; done
cd deployment
make init
```

Danach: API-Docs http://localhost:8000/docs, Health http://localhost:8000/health.

## Befehle

Alle `make`-Befehle laufen in `deployment/`.

| Befehl | Wirkung |
|---|---|
| `make dev-logs-backend` | Backend-Logs verfolgen |
| `make dev-restart-backend` | Backend neu starten |
| `make shell-backend` | bash im Container (`/app`, Code per Volume eingehängt, Hot-Reload) |
| `make migrate-dev` | `alembic upgrade head`, setzt Login der DB-Rolle `appstore_worker`, startet den Worker neu |
| `make migration-create MSG="text"` | Migration aus den Models erzeugen |
| `make migration-current` / `migration-history` / `migration-downgrade` | Revision anzeigen / Historie / eine zurück |
| `make seed-dev` | Demo-Daten (idempotent) |
| `make lint-backend` / `lint-backend-fix` / `format-backend` | ruff check / check --fix / format |

OpenAPI-Schema exportieren (in `backend/`):

```bash
make openapi    # schreibt openapi.json (im Container; MODE=host für lokales Poetry)
```

## Tests

CI führt `pytest -m unit` und `pytest -m integration` aus. Jede Testdatei braucht `pytestmark = pytest.mark.unit` (ohne DB, Ordner `tests/unit/`) oder `pytest.mark.integration`; sonst bricht die Sammlung ab (`tests/conftest.py`).

Zwei Fallen im Dev-Container:

1. Integrationstests leeren die Tabellen der Datenbank, auf die sie zeigen (`TRUNCATE`). Ohne `TEST_DATABASE_URL` ist das die Dev-Datenbank.
2. Die Umgebung des Containers (`LTI13_SESSION_SECRET`, `KEYCLOAK_*` …) schlägt in die Tests durch; ein einfaches `poetry run pytest` im Container liefert Fehlschläge. Die Tests brauchen die Umgebung von CI: nur `DATABASE_URL`, `CREDENTIAL_ENCRYPTION_KEY`, `DISABLE_BACKGROUND_TASKS`.

In `deployment/` erledigt das `make test-backend` (und `make test-backend-cov`): legt `backend_test` beim ersten Lauf an, läuft mit der Umgebung von CI und stellt danach das Login der Rolle `appstore_worker` wieder her. Das ist nötig, weil `tests/test_migrations.py` das Passwort dieser clusterweiten Rolle setzt und neue Verbindungen des Dev-Workers sonst scheitern. Der Befehl dahinter:

```bash
docker compose -f docker-compose.dev.yml exec -T postgres psql -U postgres -c "CREATE DATABASE backend_test"   # nur beim ersten Mal
docker compose -f docker-compose.dev.yml exec -T backend env -i HOME=/root \
  PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin \
  DATABASE_URL=postgresql://postgres:postgres@postgres:5432/backend_test \
  CREDENTIAL_ENCRYPTION_KEY=Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U= \
  DISABLE_BACKGROUND_TASKS=1 \
  poetry run pytest -m "unit or integration" -q --no-cov
# danach: Login der Worker-Rolle wiederherstellen
docker compose -f docker-compose.dev.yml exec -T backend poetry run python -m app.worker_db_role
```

Ergebnis mit dem Stand `dev`: 996 Tests bestanden.

`tests/test_migrations.py` legt sich selbst eine Wegwerf-Datenbank an (braucht das Recht, Datenbanken anzulegen; sonst wird der Test übersprungen).

## Konfiguration (Umgebungsvariablen)

`app/config.py`. `DATABASE_URL` und `CREDENTIAL_ENCRYPTION_KEY` sind Pflicht. `APP_ENV` ist standardmäßig `production`; dann verweigert der Start unsichere Werte (Testschlüssel, `http`-Origins, `KEYCLOAK_ISSUER` ohne `https://`).

| Variable | Bedeutung | Standard |
|---|---|---|
| `APP_ENV` | `development` erlaubt `X-Dev-User`, Docs unter `/docs`, `http`-Cookies | `production` |
| `DATABASE_URL` | Anwendungs-Postgres | – |
| `CELERY_BROKER_URL` | leer: `pgq+` + `DATABASE_URL` | leer |
| `CREDENTIAL_ENCRYPTION_KEY` | Fernet-Schlüssel; versiegelt Task-Ergebnisse, solange `RESULTS_ENCRYPTION_KEY` fehlt, und öffnet früher damit versiegelte | – |
| `RESULTS_ENCRYPTION_KEY` | Fernet-Schlüssel der Task-Ergebnisse, gleich im Worker; leer: Credential-Schlüssel; in Produktion ungleich diesem | leer |
| `WORKER_DB_PASSWORD` | Passwort der Rolle `appstore_worker` (nur für `python -m app.worker_db_role`) | leer |
| `KEYCLOAK_SERVER_URL` / `KEYCLOAK_ISSUER` | interne URL für Schlüssel und User-Suche / öffentliche Issuer-URL der Tokens | `http://keycloak:8080` / abgeleitet |
| `KEYCLOAK_REALM`, `KEYCLOAK_CLIENT_ID`, `KEYCLOAK_CLIENT_SECRET` | Realm und Service-Client | `dhbw`, `appstore-backend`, leer |
| `OIDC_ALLOWED_CLIENTS` / `OIDC_AUDIENCE` | erlaubte `azp`-Clients (kommagetrennt) / erforderliches `aud` | `appstore-frontend` / leer |
| `CORS_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, `APP_BASE_URL` | erlaubte Browser-Origins für Schreibzugriffe | localhost:3000/5173 |
| `APP_GIT_ALLOWED_HOSTS` | Hosts für App-Repositories | `github.com` |
| `APP_IMAGE_REGISTRY_ALLOWLIST`, `APP_MAX_CPU/MEMORY/STORAGE`, `APP_VM_IMAGE_NAMESPACES` | Grenzen für `appstore.yaml` | `ghcr.io/dhbw-appstore-t3/apps/`, `2`, `4Gi`, `20Gi`, `vm-poc` |
| `K8S_STATUS_ENABLED`, `K8S_ZONE`, `K8S_APP_DOMAIN`, `K8S_ENVIRONMENT` | Live-Status der Pod-Deployments aus dem Cluster | aus / leer |
| `K8S_MAX_WORKLOADS_PER_DEPLOYMENT` / `_PER_OWNER` | Obergrenzen | 60 / 0 (unbegrenzt) |
| `CLAMD_HOST`, `CLAMD_PORT`, `CLAMD_TIMEOUT` | ClamAV für Uploads; Host gesetzt: Scan Pflicht, Upload 503 bei ausgefallenem Daemon | leer, 3310, 20 |
| `LTI13_*` | LTI-1.3-Start aus Moodle; an, sobald `LTI13_SESSION_SECRET` nicht der Platzhalter ist | aus |

## Code-Struktur

Schichten: `routers/` (HTTP, Auth) → `crud/` (DB) → `models.py`; Fachlogik in `services/`.

```
app/
├── main.py            App, Router, Lifespan (Event-Hub, Reconciler), /health
├── config.py          Settings
├── database.py        Engine, Session
├── models.py          ORM
├── schemas.py         Pydantic
├── celery_app.py      Producer; Routing pods.* → Queue pods
├── pgq.py             Kombu-Transport auf Postgres (identisch im Worker)
├── task_contract.py   Event-/Ergebnis-Vertrag API ↔ Worker (identisch im Worker)
├── cli.py             python -m app.cli seed
├── k8s/               appstore.yaml-Spec, Renderer, Kube-Client, FakeKube
├── routers/ crud/ services/ utils/
└── worker_db_role.py, k8s_orphans.py,
    seal_legacy_results.py, backfill_approval_commits.py   (Betriebsbefehle, `python -m app.<name>`)
```

`app/pgq.py` und `app/task_contract.py` sind byte-gleich mit dem Worker-Repo; `tests/unit/test_shared_files.py` prüft den Hash.

### Endpunkte

Gruppen mit Prefix (`main.py`). Den vollständigen Vertrag liefert `make openapi`; unter `APP_ENV=production` sind `/docs`, `/redoc` und `/openapi.json` abgeschaltet.

| Prefix | Inhalt |
|---|---|
| `/auth` | `GET /health` |
| `/users` | `GET /me`, Liste, `/search`, `/{id}`, `/{id}/statistics`, `PUT /{id}` (Rollenvergabe durch Admin) |
| `/courses` | CRUD, Mitglieder (`/users`), Lehrende (`/teachers/{user_id}`) |
| `/teams` | CRUD, Mitglieder (`/{id}/users/{user_id}`) |
| `/apps` | Katalog, `/{id}/variables`, `/{id}/runtime`, `/{id}/versions`, Version einreichen/zurückziehen (`/versions/{tag}/submit`), Soft-Delete |
| `/admin` | `GET /apps/versions/pending`, `approve` / `reject` / `revoke` je Version, `PUT /apps/{id}` (Deaktivieren), `/recommended`, `/deploy-version` |
| `/deployments` | CRUD, `/cancel`, `/pause`, `/resume`, `/resources[/{address}[/redeploy]]`, `/workloads/{name}/restart` und `/reset`, `/files/{var}/{slot}`, `/my-access`, `/stream` (SSE) |
| `/tasks` | `/deployment/{id}`, `/{task_id}` |
| `/dashboard` | `GET /stats` |
| `/lti13` | `GET`/`POST /login`, `POST /launch`, `POST /session`, `GET /jwks` |
| `/health` | Liveness |

### Services

| Datei | Zweck |
|---|---|
| `task_service` | Task-Zeile in der Transaktion anlegen, nach Commit in `celery_queue` senden, geparkte Nachrichten freigeben |
| `task_events` | eine LISTEN-Verbindung je Prozess, Backfill für SSE (`Last-Event-ID`) |
| `task_finalizer` | Folgearbeit genau einmal (Soft-Delete nach Destroy) |
| `task_results` | versiegelte Outputs/State lesen |
| `reconciler` | tote Worker (`worker_lost`), verlorene Dispatches, geparkte Nachrichten, Aufbewahrung der Events (7 Tage) |
| `lifecycle` | erlaubte Aktionen je Deployment-Status |
| `app_spec` | `appstore.yaml` am freigegebenen Commit lesen, prüfen, Runtime bestimmen |
| `git_service` | App-Repositories lesen (Tags, Dateien) |
| `k8s_status` | Live-Status der Pods |
| `deployment_input` | Eingaben prüfen |
| `upload_validation` / `upload_formats` / `malware_scan` | Uploads prüfen (Format, ClamAV) |
| `access` | Zugangsdaten eines Mitglieds, exakt zugeordnet |
| `lti13_service` | LTI-1.3-Validierung und Session-Token |

## Laufzeit-Hinweise

- Produktions-Image: `uvicorn` als UID 10001, Port 8000, Healthcheck `GET /health`. Migrationen laufen in Kubernetes als Helm-Hook vor jedem Sync.
- `DISABLE_BACKGROUND_TASKS=1` schaltet Event-Hub und Reconciler ab (Tests).
- Doku für Mitwirkende: `claude_docs/`.
