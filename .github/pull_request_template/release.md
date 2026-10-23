## Release `dev` → `main`

<!--
Release-PR: bringt den aktuellen Stand von `dev` in Produktion. Nach dem
Merge pusht CI das Image als `latest` und startet `deployment/production.yml`
(Terraform apply + Ansible inkl. `alembic upgrade head`). Jeder Merge auf
`main` in backend, worker oder frontend löst ein vollständiges
Produktions-Deployment mit den jeweils aktuellen `latest`-Images aus.

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

**Staging:** <!-- Link zum Staging-Deploy-Lauf und zum Hermes-Report für diesen `dev`-Stand -->

**Release-Reihenfolge:** <!-- Nur falls worker/frontend/deployment mitziehen, z. B. "1. backend, 2. worker, 3. frontend". Sonst "nur backend". -->

**Rollback auf:** <!-- Aktuell produktives Image-Tag (`sha-…`) und aktuelle Alembic-Revision -->

## Release-Checkliste

<!--
Jeder Punkt wird abgehakt, bevor der PR gemergt werden kann. Trifft ein
Punkt nicht zu: mit "n/a" markieren UND abhaken, z. B.
- [x] n/a — Datenbank: keine neuen Migrationen

Die Änderungs-Checklisten der enthaltenen PRs werden hier nicht wiederholt.
Dieser PR prüft, ob der gesammelte Stand in Produktion darf. Lint, Tests,
Coverage-Gate (≥ 70 % bei PRs nach `main`), Security, Build und Image Scan
laufen als CI-Checks.
-->

- [ ] Staging: Genau dieser `dev`-Stand ist auf Staging deployt; Staging-Deploy grün, Hermes-Healthcheck GUT; die enthaltenen Änderungen dort stichprobenartig geprüft
- [ ] Enthaltene PRs: Liste oben vollständig; jeder enthaltene PR hat eine vollständige Checkliste; PRs, die ohne Review auf `dev` gemergt wurden, sind in diesem PR reviewt
- [ ] Datenbank: Neue Migrationen sind oben gekennzeichnet, liefen auf Staging durch und haben einen getesteten `downgrade`; vor destruktiven Migrationen (Spalten/Tabellen löschen) wird die Produktions-DB gesichert (`pg_dump`)
- [ ] Kompatibilität: Änderungen an API-Contract oder Celery-Tasks passen zum produktiven worker und frontend, auch zwischen den einzelnen Release-Merges; die Reihenfolge ist oben festgelegt
- [ ] Konfiguration: Neue Umgebungsvariablen sind in `deployment/docker-compose.prod.yml` durchgereicht (auf `deployment/main`) und ihre Werte im Secret `PRODUCTION_ENV_FILE` gesetzt, bevor gemergt wird
- [ ] Rollback: Image-Tag und Alembic-Revision für den Rückweg sind oben notiert (`deployment/claude_docs/rollback/`)
