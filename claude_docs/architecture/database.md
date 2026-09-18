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
- `Task` — Celery-Task-Tracking (`TaskType`, `TaskStatus`-Enums),
  separat von der eigentlichen Celery-Queue — das ist der
  DB-seitige Status, den das Frontend abfragt
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

## Bekannte Namensmuster

Migrationsdateien: `YYYY_MM_DD_HHMM-<hash>_<beschreibung>.py`. Mehrere
Revisionen heißen nur `redeploy` — das deutet auf Migrations-Merges bei
divergierenden Branches hin, kein Einzelfall in diesem Projekt (siehe
auch `claude_docs/log/` für Remote-Divergenz-Vorfälle im
`deployment`-Repo).
