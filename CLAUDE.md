# backend

Teil der DHBW-AppStore-T3 Organisation — sechs eigenständige Repos, kein Monorepo.
Harness-Gesamtkonzept: https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md

- Übergabedokument (Ist-Zustand): `claude_docs/HANDOVER.md`
- README: Start, Befehle, Konfiguration, Endpunkte, Module
- API-Contract: FastAPI ist die Quelle; Export mit `make openapi` bzw. `poetry run python scripts/export_openapi.py`
- Architektur: `claude_docs/architecture/`, Entscheidungen: `claude_docs/decisions/`, Fehlersuche: `claude_docs/debugging/`

## Befehle
Im Container (`make shell-backend` in `deployment/`) oder mit lokalem Poetry:
- Einzeltest: `poetry run pytest tests/test_foo.py -v` (`-k "name"`)
- CI-Läufe: `poetry run pytest -m unit`, `poetry run pytest -m integration`
- Lint: `poetry run ruff check .`; Format: `poetry run ruff format --check .`
- Tests löschen Tabellen der Datenbank hinter `TEST_DATABASE_URL` (ohne: Dev-DB); siehe README
