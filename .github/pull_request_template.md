## Zusammenfassung

<!-- Was ändert dieser PR und warum? 1–3 Sätze. -->

Closes #

**Art der Änderung:** <!-- feat | fix | refactor | migration | test | docs | ci | chore; bei Breaking Change (API, DB, Task-Contract) zusätzlich "BREAKING" -->

## Prüfung

<!-- Wie wurde das Verhalten geprüft? Welche Endpunkte, Rollen und Umgebung (lokal mit `deployment` → `make init` (k3d-Cluster), Swagger UI, Frontend, Staging)? -->

## Checkliste

<!--
Checkliste für Änderungs-PRs (Feature-/Fix-Branch → `dev`, Hotfix → `main`).
Release-PRs (`dev` oder `promote/*` → `main`) verwenden stattdessen
`.github/pull_request_template/release.md`.

Jeder Punkt wird abgehakt, bevor der PR gemergt werden kann. Der CI-Check
"PR Checklist" blockiert den Merge, solange hier noch ein offenes "- [ ]" steht.

Trifft ein Punkt nicht zu: mit "n/a" markieren UND abhaken, z. B.
- [x] n/a, Datenbank: keine Schemaänderung

Lint,
Tests (unit/integration), Coverage-Gate (≥ 80 % Zeilen und Branches auf jedem
Branch), Security, Build und Image Scan laufen als CI-Checks und werden hier
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
- [ ] Datenbank: Schemaänderung mit Alembic-Migration in `alembic/versions/`; `upgrade` und `downgrade` lokal mit bestehenden Daten getestet; Rechte der Worker-Rolle `appstore_worker` bedacht (Migration vergibt sie)
- [ ] Worker: Änderungen an `app/task_contract.py` oder `app/k8s/` (Task-Typen, Payloads, Events, Status) sind im Worker-Repo identisch nachgezogen (`tests/unit/test_shared_files.py` prüft den Hash), sonst Worker-PR verlinkt
- [ ] Konfiguration: Neue Umgebungsvariablen in `app/config.py` (mit sinnvollem Default) und im `deployment`-Repo nachgezogen (Chart `k8s/charts/appstore` und `k8s/environments/`; der lokale Cluster nutzt dasselbe Chart mit den Werten in `k8s/local/up.sh`)

**Sicherheit & Doku**

- [ ] Sicherheit: Keine Secrets im Diff; keine Credentials oder Tokens in Logs oder API-Responses; neue `pip-audit`-/`.trivyignore`-Ausnahmen begründet
- [ ] Doku: `claude_docs/HANDOVER.md` aktualisiert (u. a. Alembic-Head, falls eine Migration dazukommt); `README.md` (Befehle, Konfiguration, Endpunkte) und `claude_docs/` (Architektur, Entscheidungen) nachgezogen, falls betroffen; bei geändertem Verhalten, Datenmodell oder Ablauf auch die Org-Doku in `DHBW-AppStore-T3/.github` unter `docs/` (Endnutzer-Doku, Entwicklerguide, ER- und Sequenzdiagramme)
