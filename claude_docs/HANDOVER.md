# Handover — Backend

Lebendes Übergabedokument gemäß [HARNESS.md](https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md) Abschnitt 1.2.
Jede Session liest dieses Dokument zu Beginn und aktualisiert es vor dem Abschluss.

---

## 1. Status & Fokus

- **Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, Celery Producer (auf `spike/dev` über den Postgres-Transport `pgq` statt RabbitMQ/Redis, .github#5), python-keycloak.
- **Die 2 Flows (Harness Engineering):**
  - **Flow 1 (`/user-story`):** Interaktiver Dialog spezifiziert API-Endpoints (OpenAPI 3.1 Schemas) und DB-Modelle für neue Features vorab.
  - **Flow 2 (`/harness-workflow`):** Autonome Umsetzung gegen `dev`-Branch -> TDD (pytest) -> PR auf `dev` -> CI grün -> Auto-Merge -> Automatisches Staging Deployment -> Hermes Discord Statusmeldung.
  - **Push auf `main`:** Bleibt rein menschlich! Durch automatisiertes **Test Coverage Gate** abgesichert (Coverage >= 70% erforderlich).
- **API-Contract:** OpenAPI-first über `scripts/export_openapi.py` / `make openapi`. Keine veraltenden manuellen Listen mehr.
- **CI/CD:** `ci.yml` unterstützt jetzt `main` und `dev`. Push auf `dev` baut Images und stößt Staging-Deploy in `DHBW-AppStore-T3/deployment` an.

---

## 2. In Arbeit & Nächste Schritte

- [x] Reengineering auf 2 Flows: `ci.yml` auf `dev`-Trunk und Test Coverage Gate für `main` umgestellt.
- [x] Staging-Trigger korrigiert auf `DHBW-AppStore-T3/deployment --ref dev`.
- [x] OpenAPI-Export im CI (`export_openapi.py`) und Artefakt-Upload aktiv.

---

## 3. Bekannte Fallstricke & Blocker

1. **TestClient Lifespan:** `DISABLE_BACKGROUND_TASKS=1` muss bei Tests gesetzt sein, da sonst Event-Hub (LISTEN) und Reconciler gestartet werden und den Connection-Pool erschöpfen.
2. **Settings-Validierung:** `DATABASE_URL` und `CREDENTIAL_ENCRYPTION_KEY` sind Pflicht-Settings; `export_openapi.py` fängt dies mit sicheren Dummy-Fallbacks ab.
3. **Coverage Gate:** PRs auf `main` blockieren, wenn die kombinierte Unit+Integration Test Coverage unter 70% fällt.
4. **Sicherheits-Checks CI:** Trivy scannt das Docker-Image auf CVEs; bekannte Ausnahmen werden über `.trivyignore` gepflegt.
5. **Gemeinsamer Code mit dem Worker:** `app/pgq.py` und `app/task_contract.py` sind identisch im Worker-Repo; `tests/unit/test_shared_files.py` prüft den Hash in beiden Repos.
6. **`now()` in Tests:** ist in Postgres der Beginn der Transaktion. Wer Sichtbarkeiten (`visible_after <= now()`) in einer langlebigen Test-Session prüft, vorher `rollback()`.

---

## 4. Letzte Übergaben (Historie)
- **2026-10-09 (.github#5 Postgres-Queue + .github#7 K, Branch `spike/dev`):** Indizes aus `73fd123a60aa` wiederhergestellt und am Modell deklariert. Celery über `pgq` (`app/pgq.py`), kein Result-Backend, kein Celery-Event-Listener, kein In-Process-Pub/Sub mehr. SSE aus `task_events` mit `Last-Event-ID`, Cancel über die DB (geparkter Destroy), Finalizer genau einmal, Reconciler für `worker_lost`/verlorene Dispatches, versiegelte Ergebnisse (`task_results.py`), Rolle `appstore_worker` + `app/worker_db_role.py`. Ende-zu-Ende lokal geprüft (API + Worker + Postgres, Fake-Terraform): Deploy mit Live-Stream, my-access, Destroy, Cancel während Apply. ADR: `deployment/claude_docs/decisions/2026-postgres-queue.md`.

- **2026-09-24 (LTI 1.3 + Handoff, backend#9):** LTI-1.1-Launch-Pfad durch vollständigen LTI-1.3-Launch ersetzt (`app/routers/lti13.py`, `app/services/lti13_service.py`, Port von `feat/identity` — LTI 1.1 existierte auf `dev` ohnehin nicht, daher kein Removal nötig). Neuer produktionssicherer Identity-Einstiegspunkt für self-service-ui: `POST /handoff/mint` (`app/routers/handoff.py`, `app/services/handoff_service.py`), eigener `HANDOFF_SESSION_SECRET` getrennt von `LTI13_SESSION_SECRET`. `get_current_user_keycloak` prüft jetzt zuerst einen LTI-Session-Token, bevor auf Keycloak zurückgefallen wird — dafür `credentials` auf `optional_security` (statt `security`) umgestellt. Rollen-Monotonie (`higher_role()`) neu eingeführt in `keycloak_auth.py` und in `sync_user_from_keycloak` verdrahtet — vorher konnte ein Keycloak-Login eine von Moodle vergebene TEACHER-Rolle unbemerkt auf STUDENT zurücksetzen, da `sync_user_from_keycloak` die Rolle bisher bedingungslos überschrieb. Task 7 (Live-Verifikation gegen echtes `moodle_appstore`) bewusst nicht durchgeführt — braucht eine laufende Moodle-Instanz, als offenes Akzeptanzkriterium im PR dokumentiert. Bekannter, vorbestehender Fund (nicht dieser PR): `tests/test_lifecycle.py::test_in_flight_statuses_set_is_complete` schlägt bereits auf `dev` fehl (aus `feat(cancel-deployment)`, PR #7) — `running` erlaubt fälschlich eine `RESUME`-Action.
- **2026-09-18 (Harness 2-Flow Reengineering):** `ci.yml` für `dev`-Branch und Staging-Deploy-Trigger konfiguriert; Test Coverage Gate für `main` integriert; `HANDOVER.md` aktualisiert.
- **2026-09-18:** OpenAPI-Schema-Export-Validierung und Artefakt-Upload in `ci.yml` eingebunden (#3).
- **2026-09-17:** `scripts/export_openapi.py` und `make openapi` als automatisierter API-Contract eingeführt. `claude_docs/HANDOVER.md` und `CLAUDE.md` angelegt.
