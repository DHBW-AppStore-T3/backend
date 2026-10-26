# Lokales Setup — Stolpersteine

- Das Backend startet nur im Stack aus `deployment/` (`make init`, später `make dev-up`). Anleitung ab Null: `deployment/docs/dev-setup.md`.
- Abhängigkeiten verwaltet Poetry (`pyproject.toml`, `poetry.lock`); eine `requirements.txt` gibt es nicht. Im Dev-Container liegt das venv in `/app/.venv` (anonymes Volume, nicht vom Host überschrieben); Befehle immer mit `poetry run …`.
- Der Code des Hosts ist per Volume in `/app` eingehängt; `uvicorn --reload` lädt Änderungen. Änderungen am Worker brauchen `make dev-restart-worker`.
- Lokal gibt es eine einzige Postgres (`postgres-sim`, Datenbank `backend_dev`, Benutzer `postgres`). Die Worker-Rolle `appstore_worker` bekommt ihr Login erst durch `make migrate-dev`.
- Keycloak-Tokens enthalten `iss = http://localhost:8081/realms/dhbw`; das Backend erreicht Keycloak intern als `http://keycloak-sim:8081` (`KEYCLOAK_SERVER_URL`). Beide Werte setzt die Compose-Datei.
- Port 8000 belegt: `BACKEND_PORT=8010 VITE_API_URL=http://localhost:8010 make init`.
- App-Repositories klont/liest das Backend anonym von `github.com` (`APP_GIT_ALLOWED_HOSTS`); es gibt keinen Git-Token.
- Security-Scan: `pip-audit` und Trivy laufen in CI (`.github/workflows/ci.yml`); Ausnahmen für Trivy stehen in `.trivyignore`.
- `Dockerfile.dev` installiert mit `poetry.lock`: der Dev-Container hat dieselben Paketversionen wie CI und Image. Nach Änderungen an `poetry.lock` `make dev-build`.
- Die Umgebung des Dev-Containers verfälscht Tests. Tests nur über `make test-backend` (in `deployment/`) oder das Rezept im `README.md`.
- `tests/test_migrations.py` ändert das Passwort der clusterweiten Rolle `appstore_worker`: neue Verbindungen des Dev-Workers scheitern, Deployments bleiben `pending`. Abhilfe: `make migrate-dev`.
