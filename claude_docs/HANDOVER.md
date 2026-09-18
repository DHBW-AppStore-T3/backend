# Handover — Backend

Lebendes Übergabedokument gemäß [HARNESS.md](https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md) Abschnitt 1.2.
Jede Session liest dieses Dokument zu Beginn und aktualisiert es vor dem Abschluss.

---

## 1. Status & Fokus

- **Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, Celery Producer, python-keycloak.
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

1. **TestClient Lifespan:** `DISABLE_BACKGROUND_TASKS=1` muss bei Tests gesetzt sein, da sonst der Celery-Listener-Thread und Reconciler gestartet werden und den Connection-Pool erschöpfen.
2. **Settings-Validierung:** `DATABASE_URL` und `CREDENTIAL_ENCRYPTION_KEY` sind Pflicht-Settings; `export_openapi.py` fängt dies mit sicheren Dummy-Fallbacks ab.
3. **Coverage Gate:** PRs auf `main` blockieren, wenn die kombinierte Unit+Integration Test Coverage unter 70% fällt.
4. **Sicherheits-Checks CI:** Trivy scannt das Docker-Image auf CVEs; bekannte Ausnahmen werden über `.trivyignore` gepflegt.

---

## 4. Letzte Übergaben (Historie)

- **2026-09-18 (Harness 2-Flow Reengineering):** `ci.yml` für `dev`-Branch und Staging-Deploy-Trigger konfiguriert; Test Coverage Gate für `main` integriert; `HANDOVER.md` aktualisiert.
- **2026-09-18:** OpenAPI-Schema-Export-Validierung und Artefakt-Upload in `ci.yml` eingebunden (#3).
- **2026-09-17:** `scripts/export_openapi.py` und `make openapi` als automatisierter API-Contract eingeführt. `claude_docs/HANDOVER.md` und `CLAUDE.md` angelegt.
