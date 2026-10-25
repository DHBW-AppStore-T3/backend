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
- **2026-10-25 (Nachtrag zu backend#32, Branch `fix/deploy-version-stale-tag`):** `GET /apps/{id}` meldet `deploy_version` nur noch, solange die Version deploybar ist: der Tag existiert und zeigt auf den freigegebenen Commit (`_effective_deploy_version`, aus den ohnehin geladenen Tags und `approved_commits`). Verschobener oder gelöschter Tag: `null` (für alle Rollen); Repo nicht lesbar: der gespeicherte Wert. Gesetzt wird per `set_committed_value`, die Zeile bleibt unverändert (ein GET schreibt nicht). `GET /apps/` meldet weiter den gespeicherten Wert, denn dort werden keine Tags gelesen. Bei Freigabe-Ende (Widerruf, erneutes Einreichen) setzt weiterhin das Backend die Spalte zurück.
- **2026-10-25 (backend#32, Branch `feat/apps-deploy-version`, baut auf `feat/apps-recommended` auf):** `apps.deploy_version` (String, nullable, Migration `e7a9b1d3f5c7` auf `d6f8a0c2e4b6`) und `PUT /admin/apps/{app_id}/deploy-version` (nur Admin, Body `{"version_tag": str | null}`; `null` löscht die Festlegung). Gesetzt werden kann nur eine Version mit Status `approved` und Commit (sonst 404 ohne Eintrag, 409 `version_not_approved`) **deren Tag noch auf den freigegebenen Commit zeigt** (409 `version_moved`, wie bei der Freigabe; kostet einen Git-Zugriff, 502 wenn das Repo nicht lesbar ist). Die Freigabezeile ist beim Prüfen gesperrt (`get_approval(lock=True)`), ein gleichzeitiger Widerruf wartet und löscht die Festlegung danach. Zurückgesetzt wird `deploy_version`, sobald die Freigabe der Version endet: beim Widerruf **und** wenn `submit_version` den Eintrag durch eine neue Einreichung ersetzt (verschobener Tag), beides über `_clear_deploy_version`. `POST /deployments/` ist nicht an die Festlegung gebunden. `AppCreate`/`AppUpdate` haben das Feld nicht. Alembic-Head jetzt `e7a9b1d3f5c7`; PR #39 hängt `c5e7a9b1d3f5` an `b4d6f8a0c2e4`: wer später merged, setzt `down_revision` neu. Frontend: frontend#56.
- **2026-10-25 (backend#31, Branch `feat/apps-recommended`):** `apps.recommended` (Boolean, Standard `false`, Migration `d6f8a0c2e4b6` auf `b4d6f8a0c2e4`) und `PUT /admin/apps/{app_id}/recommended` (nur Admin, Body `{"recommended": bool}`, Antwort `AppResponse`). Das Feld steht in `AppResponse` (und damit in `AppWithUser`/`AppWithVersions`), aber bewusst weder in `AppCreate` noch in `AppUpdate`: Besitzer können es nicht setzen. Der Alembic-Head ist jetzt `d6f8a0c2e4b6`; PR #39 hängt `c5e7a9b1d3f5` an `b4d6f8a0c2e4` — wer als Zweites merged, setzt `down_revision` neu (`alembic heads` muss eine Zeile zeigen). Das Frontend-Issue „Filter Empfohlen" (frontend#55) liest das Feld nach `make openapi`.
- **2026-10-09 (.github#5 Postgres-Queue + .github#7 K, Branch `spike/dev`):** Indizes aus `73fd123a60aa` wiederhergestellt und am Modell deklariert. Celery über `pgq` (`app/pgq.py`), kein Result-Backend, kein Celery-Event-Listener, kein In-Process-Pub/Sub mehr. SSE aus `task_events` mit `Last-Event-ID`, Cancel über die DB (geparkter Destroy), Finalizer genau einmal, Reconciler für `worker_lost`/verlorene Dispatches, versiegelte Ergebnisse (`task_results.py`), Rolle `appstore_worker` + `app/worker_db_role.py`. Ende-zu-Ende lokal geprüft (API + Worker + Postgres, Fake-Terraform): Deploy mit Live-Stream, my-access, Destroy, Cancel während Apply. ADR: `deployment/claude_docs/decisions/2026-postgres-queue.md`.

- **2026-09-24 (LTI 1.3 + Handoff, backend#9):** LTI-1.1-Launch-Pfad durch vollständigen LTI-1.3-Launch ersetzt (`app/routers/lti13.py`, `app/services/lti13_service.py`, Port von `feat/identity` — LTI 1.1 existierte auf `dev` ohnehin nicht, daher kein Removal nötig). Neuer produktionssicherer Identity-Einstiegspunkt für self-service-ui: `POST /handoff/mint` (`app/routers/handoff.py`, `app/services/handoff_service.py`), eigener `HANDOFF_SESSION_SECRET` getrennt von `LTI13_SESSION_SECRET`. `get_current_user_keycloak` prüft jetzt zuerst einen LTI-Session-Token, bevor auf Keycloak zurückgefallen wird — dafür `credentials` auf `optional_security` (statt `security`) umgestellt. Rollen-Monotonie (`higher_role()`) neu eingeführt in `keycloak_auth.py` und in `sync_user_from_keycloak` verdrahtet — vorher konnte ein Keycloak-Login eine von Moodle vergebene TEACHER-Rolle unbemerkt auf STUDENT zurücksetzen, da `sync_user_from_keycloak` die Rolle bisher bedingungslos überschrieb. Task 7 (Live-Verifikation gegen echtes `moodle_appstore`) bewusst nicht durchgeführt — braucht eine laufende Moodle-Instanz, als offenes Akzeptanzkriterium im PR dokumentiert. Bekannter, vorbestehender Fund (nicht dieser PR): `tests/test_lifecycle.py::test_in_flight_statuses_set_is_complete` schlägt bereits auf `dev` fehl (aus `feat(cancel-deployment)`, PR #7) — `running` erlaubt fälschlich eine `RESUME`-Action.
- **2026-09-18 (Harness 2-Flow Reengineering):** `ci.yml` für `dev`-Branch und Staging-Deploy-Trigger konfiguriert; Test Coverage Gate für `main` integriert; `HANDOVER.md` aktualisiert.
- **2026-09-18:** OpenAPI-Schema-Export-Validierung und Artefakt-Upload in `ci.yml` eingebunden (#3).
- **2026-09-17:** `scripts/export_openapi.py` und `make openapi` als automatisierter API-Contract eingeführt. `claude_docs/HANDOVER.md` und `CLAUDE.md` angelegt.
