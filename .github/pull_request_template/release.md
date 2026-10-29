## Release `dev` → `main`

<!--
Release-PR: bringt den aktuellen Stand von `dev` in Produktion. Der Merge
allein rollt noch nichts aus: Nach dem Merge pusht CI das Image (Tag
`sha-<Commit>`) nach ghcr.io und startet `k8s-bump.yml` im Repo `deployment`
mit Ref `main`. Das öffnet dort einen PR "chore(k8s): roll out backend
sha-… on prod", der den Digest in `k8s/images/prod.yaml` setzt. Erst wenn eine
Person diesen PR freigibt und mergt (bei `PROD_AUTO_MERGE=true` mergt er sich
selbst), rollt Argo CD (`prod-root`) aus; der Job `migrate` führt vor jedem
Sync `alembic upgrade head` aus (PreSync-Hook).

Anlegen:
  gh pr create --base main --head dev --title "release: <Datum>" \
    --body-file .github/pull_request_template/release.md
oder im Browser: .../compare/main...dev?expand=1&template=release.md

Mit Merge-Commit mergen, nicht squashen, damit `dev` und `main` nicht
auseinanderlaufen.
-->

**Enthaltene PRs:**

<!-- Alle PRs seit dem letzten Release, z. B. aus `git log --oneline origin/main..origin/dev`. BREAKING- und Migrations-PRs kennzeichnen. -->

-

**Staging:** <!-- Link zum CI-Lauf mit dem Roll-out auf Staging und Ergebnis von `staging_verdict` (appstore-mcp, Skill `/verify-staging`) für diesen `dev`-Stand -->

**Release-Reihenfolge:** <!-- Nur falls worker/frontend/deployment mitziehen, z. B. "1. backend, 2. worker, 3. frontend". Sonst "nur backend". -->

**Rollback auf:** <!-- Aktuell produktiver Image-Digest/Tag aus `deployment/k8s/images/prod.yaml` und aktuelle Alembic-Revision -->

## Release-Checkliste

<!--
Jeder Punkt wird abgehakt, bevor der PR gemergt werden kann. Trifft ein
Punkt nicht zu: mit "n/a" markieren UND abhaken, z. B.
- [x] n/a — Datenbank: keine neuen Migrationen

Die Änderungs-Checklisten der enthaltenen PRs werden hier nicht wiederholt.
Dieser PR prüft, ob der gesammelte Stand in Produktion darf. Lint, Tests,
Coverage-Gate (≥ 80 % Zeilen und Branches), Security, Build und Image Scan
laufen als CI-Checks.
-->

- [ ] Staging: Genau dieser `dev`-Stand ist auf Staging ausgerollt, CI inkl. Roll-out grün, `staging_verdict` ohne Befund; die enthaltenen Änderungen dort stichprobenartig geprüft
- [ ] Enthaltene PRs: Liste oben vollständig; jeder enthaltene PR hat eine vollständige Checkliste; PRs, die ohne Review auf `dev` gemergt wurden, sind in diesem PR reviewt
- [ ] Datenbank: Neue Migrationen sind oben gekennzeichnet, liefen auf Staging durch und haben einen getesteten `downgrade`; vor destruktiven Migrationen (Spalten/Tabellen löschen) ist die Produktions-DB von Hand gesichert, denn automatische Backups gibt es noch nicht (`deployment/claude_docs/HANDOVER.md`, Abschnitt „Offen“)
- [ ] Kompatibilität: Änderungen an API-Contract oder Task-Contract (`app/task_contract.py`) passen zum produktiven worker und frontend, auch zwischen den einzelnen Release-Merges; die Reihenfolge ist oben festgelegt
- [ ] Konfiguration: Neue Umgebungsvariablen sind im Chart `deployment/k8s/charts/appstore` (Werte in `k8s/environments/prod.yaml`) gesetzt, neue Secret-Schlüssel mit `k8s/bootstrap.sh prod` im Cluster angelegt; beides ist auf `deployment/main` (`k8s/promote.sh`), bevor der Digest-PR für Produktion gemergt wird
- [ ] Rollback: Image-Digest/Tag und Alembic-Revision für den Rückweg sind oben notiert (Rückweg: Digest in `deployment/k8s/images/prod.yaml` per PR auf `main` zurücksetzen; Migrationen laufen nicht automatisch zurück)
- [ ] Doku: `README.md`, `claude_docs/HANDOVER.md` und die Org-Doku in `DHBW-AppStore-T3/.github` unter `docs/` (Endnutzer-Doku, Entwicklerguide, ER- und Sequenzdiagramme) beschreiben den Stand, der jetzt in Produktion geht
