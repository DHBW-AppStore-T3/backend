# Alembic

Migrationen des Backends. Konfiguration: `alembic.ini`, `alembic/env.py` (liest `DATABASE_URL`), Vorlage `script.py.mako`, Revisionen in `alembic/versions/` (27, ein Head).

Befehle lokal (aus `deployment/`): `make migrate-dev`, `make migration-create MSG="…"`, `make migration-current`, `make migration-history`, `make migration-downgrade`.

Im Backend-Container (`make shell-backend`):

```bash
poetry run alembic upgrade head
poetry run alembic revision --autogenerate -m "text"
poetry run alembic heads        # genau eine Zeile
poetry run alembic check        # Models und Migrationen stimmen überein
poetry run alembic downgrade -1
```

Regeln:

- Neue Revision hängt an den einzigen Head. Bei zwei Heads setzt der später gemergte PR `down_revision` neu.
- Jede Änderung an `models.py` (auch Indizes) braucht ihre Revision; `alembic check` und `tests/test_migrations.py` schlagen sonst fehl.
- Gegen Staging und Produktion laufen Migrationen als Helm-Hook (`migrate`-Job) vor dem Sync.
