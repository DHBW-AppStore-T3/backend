# Handover — Backend

Lebendes Übergabedokument gemäß [HARNESS.md](https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md) Abschnitt 1.2.
Jede Session liest dieses Dokument zu Beginn und aktualisiert es vor dem Abschluss.

---

## 1. Status & Fokus

- **Stack:** FastAPI, SQLAlchemy 2.0, PostgreSQL, Celery Producer, python-keycloak.
- **API-Contract:** OpenAPI-first über `scripts/export_openapi.py` / `make openapi`. Keine veraltenden manuellen Markdown-Listen mehr.
- **CI:** Lint (Ruff), Test (Unit + Integration gegen Postgres), Security, Docker Build, Image Scan sind Pflicht-Checks auf `main`.

---

## 2. In Arbeit & Nächste Schritte

- [x] **OpenAPI-Export im CI:** `export_openapi.py` im CI-Workflow integriert und Schema-Artefakt bereitgestellt (`.github/workflows/ci.yml`).
- [ ] **Frontend-Typengenerierung:** `frontend/` an das exportierte `openapi.json` anbinden (z. B. via `openapi-typescript`).

---

## 3. Bekannte Fallstricke & Blocker

1. **TestClient Lifespan:** `DISABLE_BACKGROUND_TASKS=1` muss bei Tests gesetzt sein, da sonst der Celery-Listener-Thread und Reconciler gestartet werden und den Connection-Pool erschöpfen.
2. **Settings-Validierung:** `DATABASE_URL` und `CREDENTIAL_ENCRYPTION_KEY` sind Pflicht-Settings; `export_openapi.py` fängt dies mit sicheren Dummy-Fallbacks ab.
3. **Sicherheits-Checks CI:** Trivy scannt das Docker-Image auf CVEs; bekannte Ausnahmen werden über `.trivyignore` gepflegt.

---

## 4. Letzte Übergaben (Historie)

- **2026-09-18:** OpenAPI-Schema-Export-Validierung und Artefakt-Upload in `ci.yml` eingebunden.
- **2026-09-17:** `scripts/export_openapi.py` und `make openapi` als automatisierter API-Contract eingeführt. `claude_docs/HANDOVER.md` und `CLAUDE.md` angelegt.
