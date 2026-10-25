# Bekannte Fehlerbilder

## `python -m` statt direktem Console-Script

Das Backend-Image baut sein venv unter `/build/.venv`, kopiert es dann
nach `/app/.venv` — dadurch tragen die Console-Scripts (`uvicorn`,
`alembic`) einen stale Build-Zeit-Shebang (`#!/build/.venv/bin/python`),
der zur Laufzeit nicht auflöst. Deshalb startet
`docker-compose.prod.yml` den Server über
`python -m uvicorn app.main:app ...` statt `uvicorn ...` direkt — bei
neuen Skripten/Cronjobs im Container dasselbe Muster verwenden, sonst
schlägt der Aufruf mit "no such file or directory" fehl, obwohl die
Datei existiert.

## `CREDENTIAL_ENCRYPTION_KEY` fehlt

Backend und Worker teilen sich diesen Fernet-Key (er versiegelt die
Task-Ergebnisse, solange kein `RESULTS_ENCRYPTION_KEY` gesetzt ist). Fehlt er, bricht der Container-Start mit
einer expliziten `:? is required`-Fehlermeldung ab (siehe
`docker-compose.prod.yml`) — kein stiller Fallback. Wenn Backend und
Worker unterschiedliche Werte haben, startet zwar beides, aber jede
Entschlüsselung schlägt zur Laufzeit fehl.

Task-Ergebnisse (`tasks.outputs_enc`) haben einen eigenen Schlüssel,
`RESULTS_ENCRYPTION_KEY` (API, VM-Executor und Pod-Deployer; der
Pod-Deployer bekommt nur ihn, in Kubernetes ohne den Credential-Key).
Ohne ihn versiegelt der Credential-Key wie bisher; Ergebnisse, die mit
ihm versiegelt wurden, öffnet die API weiterhin. In Produktion darf er
nicht gleich dem Credential-Key sein. Hat ein Worker einen anderen
Ergebnis-Schlüssel als die API, zeigt die API keine Outputs dieser Tasks.

## CORS_ORIGINS

Stand 2026-09-15 gab es einen Merge-Konflikt zwischen zwei Remotes zum
`CORS_ORIGINS`-Verhalten (siehe `deployment`-Repo,
`claude_docs/log/2026-W38.md`) — die aktuelle Prod-Config mountet die
`.env`-Datei direkt in den Container statt `CORS_ORIGINS` als
einzelne Env-Var zu setzen. Bei CORS-Fehlern zuerst prüfen, ob die
`.env`-Mount-Variante noch aktiv ist oder wieder auf eine einzelne
Env-Var umgestellt wurde.
