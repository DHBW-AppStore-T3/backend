# Handover — Backend

Lebendes Übergabedokument (Harness-Konzept: [HARNESS.md](https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md)). Beschreibt den Ist-Zustand von `dev`; Stand wird mit jeder Änderung angepasst.

## 1. Zustand

- **Stack:** FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL 16 (auch als Task-Queue: Tabelle `tasks`, LISTEN/NOTIFY), PyJWT gegen Keycloak, Python 3.11.
- **Eine Worker-Queue:** `pods` (Kubernetes- und KubeVirt-Apps). Der OpenStack-VM-Pfad ist entfernt (.github#12).
- **Live-Status:** SSE aus `task_events` (`LISTEN/NOTIFY`, `Last-Event-ID`).
- **Alembic-Head:** `c5e7a9b1d3f5`, 28 Revisionen.
- **Branches:** `dev` → Staging, `main` → Produktion. Feature-Branches gehen per PR auf `dev`.
- **Dokumentation:** `README.md` (Start, Befehle, Konfiguration, Endpunkte, Module), `claude_docs/architecture/` (Überblick, Auth, Datenbank).

## 2. CI (`.github/workflows/ci.yml`, Branches `main` und `dev`)

Lint (ruff, OpenAPI-Export als Artefakt) · Unit (`pytest -m unit`) · Integration (`pytest -m integration`, Postgres-Service) · Coverage (kombiniert; getrennte Gates ≥ 86 % Zeilen und ≥ 76 % Branches, auf allen Branches und PRs; ein rotes Gate stoppt Build, Push und Rollout) · Security (`pip-audit --strict`, Trivy-Dateisystem; Ausnahmen in `.trivyignore`) · Build · Image-Scan (Trivy) · Push nach `ghcr.io` (nur bei Push/`workflow_dispatch`) · Roll out on k8s: startet `k8s-bump.yml` im Repo `deployment` mit dem Branch des Pushes.

## 3. Regeln im Code

1. Tests brauchen `DISABLE_BACKGROUND_TASKS=1` (setzt `tests/conftest.py`).
2. Jede Testdatei trägt `pytest.mark.unit` oder `pytest.mark.integration`, sonst bricht die Sammlung ab. `unit` = ohne DB, Ordner `tests/unit/`.
3. `app/task_contract.py` und `app/k8s/` sind byte-gleich mit dem Worker-Repo (`tests/unit/test_shared_files.py` prüft den Hash). Änderung immer in beiden Repos.
4. Tests laufen mit `APP_ENV=development`; Produktionsprüfungen der Settings testen eigene `Settings`-Objekte.
5. `now()` ist in Postgres der Transaktionsbeginn; Sichtbarkeitstests in langlebigen Sessions vorher `rollback()`.
6. `AppCreate`/`AppUpdate` enthalten weder `recommended` noch `deploy_version`; nur die Admin-Endpunkte setzen sie.
7. `deploy_version` wird zurückgesetzt, sobald die Freigabe der Version endet (Widerruf, erneute Einreichung).

## 4. Offen

- Die Windows-App läuft über KubeVirt (`runtime: kubevirt-vm`); der Betrieb ist ein eigenes Thema (siehe `deployment/k8s/README.md`).
