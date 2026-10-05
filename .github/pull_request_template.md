## Zusammenfassung

<!-- Was ändert dieser PR und warum? 1–3 Sätze. -->

Closes #

**Art der Änderung:** <!-- feat | fix | refactor | migration | test | docs | ci | chore — bei Breaking Change (API, DB, Task-Payload) zusätzlich "BREAKING" -->

## Prüfung

<!-- Wie wurde das Verhalten geprüft? Welche Endpunkte, Rollen und Umgebung (lokal mit `deployment` → `make dev-up`, Swagger UI, Frontend, Staging)? -->

## Checkliste

<!--
Jeder Punkt wird abgehakt, bevor der PR gemergt werden kann. Der CI-Check
"PR Checklist" blockiert den Merge, solange hier noch ein offenes "- [ ]" steht.

Trifft ein Punkt nicht zu: mit "n/a" markieren UND abhaken, z. B.
- [x] n/a — Datenbank: keine Schemaänderung

Die reviewende Person prüft, dass die Häkchen stimmen, und bestätigt das mit
ihrem Approval. Lint, Tests (unit/integration) inkl. Coverage-Gate, Security,
Build und Image Scan werden separat als Pflicht-CI-Checks erzwungen und hier
nicht wiederholt.
-->

**Funktionale Eignung**

- [ ] Vollständigkeit: Alle Akzeptanzkriterien des verlinkten Issues sind umgesetzt; Abweichungen oder offene Punkte sind oben begründet
- [ ] Korrektheit: pytest-Tests (`unit`/`integration`) decken die Akzeptanzkriterien ab, inkl. Fehlerfällen (400/403/404/409/422, ungültige Zustandsübergänge)
- [ ] Angemessenheit: Endpunkte manuell gegen eine laufende Umgebung (lokal oder Staging) aufgerufen, nicht nur über `TestClient`
- [ ] Berechtigungen: Zugriff serverseitig pro Rolle und Scope (Kurs, Owner) durchgesetzt (`require_roles`); verbotene Zugriffe sind getestet
- [ ] Regression: Bestehende Abläufe, die geänderten Code mitnutzen, funktionieren weiterhin (z. B. Deployment anlegen → Task → Status)

**Schnittstellen & Daten**

- [ ] API-Contract: Schema-Änderungen sind abwärtskompatibel oder als BREAKING markiert; OpenAPI exportiert (`make openapi`) und Frontend-PR mit aktualisierten Typen verlinkt
- [ ] Datenbank: Schemaänderung mit Alembic-Migration in `alembic/versions/`; `upgrade` und `downgrade` lokal mit bestehenden Daten getestet
- [ ] Worker: Geänderte Celery-Tasks, Argumente oder Status-Rückmeldungen sind mit dem Worker kompatibel, sonst Worker-PR verlinkt
- [ ] Konfiguration: Neue Umgebungsvariablen in `app/config.py` (mit sinnvollem Default) und im `deployment`-Repo (`.env.example`, Compose) nachgezogen

**Sicherheit & Doku**

- [ ] Sicherheit: Keine Secrets im Diff; keine Credentials oder Tokens in Logs oder API-Responses; neue `pip-audit`-/`.trivyignore`-Ausnahmen begründet
- [ ] Doku: `claude_docs/HANDOVER.md` aktualisiert; Architektur/Entscheidungen in `claude_docs/` nachgezogen, falls betroffen
