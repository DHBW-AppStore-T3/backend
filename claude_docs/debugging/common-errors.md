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

Backend und Worker teilen sich diesen Fernet-Key zum Ver-/Entschlüsseln
von OpenStack-Credentials. Fehlt er, bricht der Container-Start mit
einer expliziten `:? is required`-Fehlermeldung ab (siehe
`docker-compose.prod.yml`) — kein stiller Fallback. Wenn Backend und
Worker unterschiedliche Werte haben, startet zwar beides, aber jede
Entschlüsselung schlägt zur Laufzeit fehl.

## CORS_ORIGINS

Stand 2026-09-15 gab es einen Merge-Konflikt zwischen zwei Remotes zum
`CORS_ORIGINS`-Verhalten (siehe `deployment`-Repo,
`claude_docs/log/2026-W38.md`) — die aktuelle Prod-Config mountet die
`.env`-Datei direkt in den Container statt `CORS_ORIGINS` als
einzelne Env-Var zu setzen. Bei CORS-Fehlern zuerst prüfen, ob die
`.env`-Mount-Variante noch aktiv ist oder wieder auf eine einzelne
Env-Var umgestellt wurde.
