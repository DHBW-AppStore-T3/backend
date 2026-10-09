# Datenbank & Migrationen

PostgreSQL, SQLAlchemy 2.0, Alembic für Migrationen (`alembic/versions/`,
18 Revisionen Stand 2026-09).

## Kern-Tabellen (`app/models.py`)

- `Course` — Kurs/Veranstaltung, hat `CourseTeacher`-Join für Dozierende
- `User` — mit `UserRole`-Enum
- `App` — Katalog-Eintrag, versioniert über `AppVersionApproval`
  (Freigabe-Workflow für neue App-Versionen)
- `Deployment` — eine laufende oder vergangene App-Instanz, verknüpft
  über `UserToDeployment`
- `Task` — der Auftrag und seine einzige Wahrheit (`TaskType`,
  `TaskStatus`-Enums). Seit .github#5 zusätzlich Lease (`claimed_by`,
  `lease_until`), `cancel_requested_at`, `finalized_at` (Folgearbeit
  erledigt) und `outputs_enc` (Fernet über Outputs + State). Höchstens
  ein PENDING/RUNNING-Task je Deployment (`uq_tasks_active_per_deployment`)
- `CeleryQueueMessage` (`celery_queue`) — die Celery-Queue selbst
  (Transport `pgq`); `after_task` parkt einen Destroy hinter einem
  abgebrochenen Job
- `TaskEvent` (`task_events`) — Live-Events eines Tasks; Trigger
  `task_events_notify` sendet `NOTIFY task_events`; 7 Tage nach Task-Ende
  gelöscht
- `Team` — Gruppierung von Nutzern, über `UserToTeam`
- `UserOpenStackCredential` — verschlüsselte OpenStack-Zugangsdaten pro
  Nutzer (`CREDENTIAL_ENCRYPTION_KEY`, geteilt mit dem Worker)

## Migrationsstrategie

Migrationen laufen **nicht** automatisch beim Container-Start — bewusst,
siehe Kommentar in `docker-compose.prod.yml` im `deployment`-Repo:
"Do not re-introduce a migrate init-container here — that pattern hides
migration failures inside the compose output." Stattdessen manuell nach
dem Deploy:

```bash
docker exec backend-prod python -m alembic upgrade head
```

Alembic-Migrationen gegen Prod/Staging brauchen laut HARNESS.md immer
menschliche Bestätigung im selben Moment, nie automatisiert durch einen
Agenten.

Nach `5e1f0c2a9b7d` braucht die Rolle `appstore_worker` ein Login:
`docker exec backend-prod python -m app.worker_db_role` (liest
`WORKER_DB_PASSWORD`). Die Tests bauen das Schema per `create_all`;
`tests/test_migrations.py` prüft die echte Kette (Upgrade, `alembic
check`, Trigger, Rechte der Rolle, Down/Up) auf einer Wegwerf-DB.

## Bekannte Namensmuster

Migrationsdateien: `YYYY_MM_DD_HHMM-<hash>_<beschreibung>.py`. Mehrere
Revisionen heißen nur `redeploy` — das deutet auf Migrations-Merges bei
divergierenden Branches hin, kein Einzelfall in diesem Projekt (siehe
auch `claude_docs/log/` für Remote-Divergenz-Vorfälle im
`deployment`-Repo).
