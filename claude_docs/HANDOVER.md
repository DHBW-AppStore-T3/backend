# Handover — Backend

Lebendes Übergabedokument (Harness-Konzept: [HARNESS.md](https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md)). Beschreibt den Ist-Zustand von `dev`; Stand wird mit jeder Änderung angepasst.

## 1. Zustand

- **Stack:** FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL 16 (auch als Task-Queue: Tabelle `tasks`, LISTEN/NOTIFY), PyJWT gegen Keycloak, Python 3.11.
- **Eine Worker-Queue:** `pods` (Kubernetes- und KubeVirt-Apps). Der OpenStack-VM-Pfad ist entfernt (.github#12).
- **Live-Status:** SSE aus `task_events` (`LISTEN/NOTIFY`, `Last-Event-ID`).
- **Alembic-Head:** `a2c4e6f8b0d2` (`deployments.k8s_scope`), 30 Revisionen.
- **Branches:** `dev` → Staging, `main` → Produktion. Feature-Branches gehen per PR auf `dev`.
- **Dokumentation:** `README.md` (Start, Befehle, Konfiguration, Endpunkte, Module), `claude_docs/architecture/` (Überblick, Auth, Datenbank).

## 2. CI (`.github/workflows/ci.yml`, Branches `main` und `dev`)

Lint (ruff, OpenAPI-Export als Artefakt) · Unit (`pytest -m unit`) · Integration (`pytest -m integration`, Postgres-Service) · Coverage (kombiniert; getrennte Gates ≥ 80 % Zeilen und ≥ 80 % Branches, auf allen Branches und PRs; ein rotes Gate stoppt Build, Push und Rollout) · Security (`pip-audit --strict`, Trivy-Dateisystem; Ausnahmen in `.trivyignore`) · Build · Image-Scan (Trivy) · Push nach `ghcr.io` (nur bei Push/`workflow_dispatch`) · Roll out on k8s: startet `k8s-bump.yml` im Repo `deployment` mit dem Branch des Pushes.

## 3. Regeln im Code

1. Tests brauchen `DISABLE_BACKGROUND_TASKS=1` (setzt `tests/conftest.py`).
2. Jede Testdatei trägt `pytest.mark.unit` oder `pytest.mark.integration`, sonst bricht die Sammlung ab. `unit` = ohne DB, Ordner `tests/unit/`.
3. `app/task_contract.py` und `app/k8s/` sind byte-gleich mit dem Worker-Repo (`tests/unit/test_shared_files.py` prüft den Hash). Änderung immer in beiden Repos.
4. Tests laufen mit `APP_ENV=development`; Produktionsprüfungen der Settings testen eigene `Settings`-Objekte.
5. `now()` ist in Postgres der Transaktionsbeginn; Sichtbarkeitstests in langlebigen Sessions vorher `rollback()`.
6. `AppCreate`/`AppUpdate` enthalten weder `recommended` noch `deploy_version`; nur die Admin-Endpunkte setzen sie.
7. `deploy_version` wird zurückgesetzt, sobald die Freigabe der Version endet (Widerruf, erneute Einreichung).
8. `scopeChoice` in `appstore.yaml` (Standard `false`, `scope` bleibt der Startwert): Nur wo der Autor es erlaubt, gilt `userInputVar.scope` (`user` oder `team`). Der Job trägt dann ein festes Spec (`scope` = die Wahl, `scopeChoice` false), sonst 422 `invalid_scope`. `GET /apps/{id}/runtime` meldet `scope_choice`. Wie bei `network` und `resources` bekommt nur der Job das geänderte Spec; das geprüfte Spec der Version bleibt unberührt. Der Validator im Repo `app-registry` kennt den Schlüssel ebenfalls (Drift-Test).
9. Was der Job ausgeführt hat, steht in `deployments.k8s_scope` (`team` oder `user`, ältere Zeilen NULL) und kommt als `scope` in Liste, Detail und POST-Antwort. Teams mit mehreren Personen und `scope: user` sind erlaubt (eine Umgebung je Person, Dateien je Team).
10. Die Teams eines Pod- oder KubeVirt-Deployments sind nach dem Anlegen fest: Schreibende Team-Endpunkte (`/teams/…`) antworten nach der Rechteprüfung mit 409 `teams_fixed`. Der Job hat Workloads und Zugänge aus diesen Teams gebaut, nichts baut sie neu; eine spätere Änderung erreichte niemanden.

## 4. Offen

- Die Windows-App läuft über KubeVirt (`runtime: kubevirt-vm`); der Betrieb ist ein eigenes Thema (siehe `deployment/k8s/README.md`).
