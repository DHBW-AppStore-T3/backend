# Backend — Überblick

FastAPI-App `app/main.py`, Poetry, Python 3.11. Vollständige Modul- und Endpunktliste: `README.md`.

## Laufzeit

- Prozess: `uvicorn app.main:app` (Produktion: UID 10001, Port 8000, mehrere Worker-Prozesse über `WORKERS`).
- Lifespan (außer `DISABLE_BACKGROUND_TASKS=1`): `hub` (`services/task_events.py`, eine LISTEN-Verbindung auf `task_events` und `task_released`) und `run_reconciler` (`services/reconciler.py`) starten je API-Prozess.
- Middleware: CORS (`CORS_ORIGINS`), `CrossSiteWriteGuard` (verweigert Schreibzugriffe fremder Origins; erlaubt: `APP_BASE_URL`, `CORS_ORIGINS`, `CSRF_TRUSTED_ORIGINS`).
- Docs (`/docs`, `/redoc`, `/openapi.json`) nur mit `APP_ENV=development`.
- Handler sind überwiegend synchron (`def`, laufen im Threadpool); `async def` nur in `deployments.py` (SSE), `lti13.py`.

## Komponenten

| Komponente | Rolle |
|---|---|
| PostgreSQL | Daten, Queue (`celery_queue`), Task-Events |
| Keycloak | Anmeldung; das Backend prüft nur Access-Tokens (siehe `auth.md`) |
| Worker (`pod-deployer`) | führt die Jobs der Queue `pods` aus: Kubernetes- und KubeVirt-Apps |
| Kubernetes-API | nur lesend (Live-Status, Orphan-Check) |
| ClamAV | optional, Virenscan der Uploads |
| Moodle | LTI-1.3-Start (`/lti13/*`) |

Kein RabbitMQ, kein Redis, kein Celery-Result-Backend, kein SMTP-Versand.

## Runtimes einer App-Version

Jede App-Version braucht eine `appstore.yaml` am Commit; ohne gültige Datei lässt sie sich weder einreichen noch freigeben noch deployen (422 `appstore_yaml_missing`). `services/app_spec.py` bestimmt die Runtime daraus:

| Runtime | Erkennung | Queue | Ausführung |
|---|---|---|---|
| `kubernetes` | `runtime: kubernetes` | `pods` | Namespace `dep-<8 hex>` |
| `kubevirt-vm` | `runtime: kubevirt-vm` und `vm` | `pods` | KubeVirt-VM im Namespace `dep-<8 hex>` |

Den früheren Pfad für Apps ohne `appstore.yaml` (OpenStack, Packer, OpenTofu, Queue `vm`) gibt es nicht mehr (.github#12).

Spec-Validierung gegen `APP_IMAGE_REGISTRY_ALLOWLIST`, `APP_MAX_*`, `APP_VM_IMAGE_NAMESPACES`. Der Worker prüft dieselben Grenzen erneut.

## Ablauf: App deployen

1. `POST /deployments/` prüft Rolle, Freigabe, Eingaben; legt `Deployment` und `Task` (PENDING) in einer Transaktion an (`task_service.prepare_task_in_tx`, höchstens ein PENDING/RUNNING-Task je Deployment, Unique-Index `uq_tasks_active_per_deployment`).
2. Nach dem Commit sendet `task_service` die Celery-Nachricht in `celery_queue` (Task-ID = `taskId`; Routing `pods.*` → `pods`).
3. Der Worker holt sie (`SKIP LOCKED`), hält eine Lease auf der Task-Zeile, schreibt Fortschritt und Logzeilen in `task_events`, am Ende Status, Transkript und versiegelte Ergebnisse (`tasks.outputs_enc`) in die Task-Zeile.
4. Trigger `task_events_notify` sendet `NOTIFY task_events`; jeder API-Prozess streamt die Events per `GET /deployments/{id}/stream` (SSE, `id:` = Event-ID, `Last-Event-ID` zum Fortsetzen).
5. Bei einem Terminal-Event weckt der Hub den Reconciler; `task_finalizer` erledigt die Folgearbeit genau einmal (Soft-Delete nach Destroy).

Abbrechen: `POST /deployments/{id}/cancel` setzt `cancel_requested_at` und sendet `NOTIFY task_cancel`; ein anschließender Destroy wird in `celery_queue.after_task` geparkt und nach der Freigabe (`task_released`) ausgeführt.

Fehlerarten (`failure_kind`): `worker_failure`, `worker_lost`, `celery_infrastructure`, `time_limit` (`task_contract.py`).

## Freigabe-Workflow

Version einreichen (`POST /apps/{id}/versions/{tag}/submit`) → Admin `approve`/`reject`/`revoke`. Die Freigabe ist an den Commit gebunden (`AppVersionApproval.commit_sha`); zeigt der Tag später auf einen anderen Commit, ist die Version nicht deploybar (409 `version_moved`). `apps.deploy_version` legt ein Admin fest (`PUT /admin/apps/{id}/deploy-version`, nur freigegebene Version mit unverändertem Tag); `GET /apps/{id}` meldet den Wert nur, solange die Version deploybar ist.

## Berechtigungen

Rollen `student` < `teacher` < `admin` (`utils/roles.py`, `utils/capabilities.py`, `utils/permissions.py`). Die wirksame Rolle ist die höchste aus `keycloak_role`, `lti_role`, `granted_role`. Rate-Limits je Person auf teuren Routen (`utils/rate_limit.py`).
