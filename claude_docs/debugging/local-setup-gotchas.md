# Lokales Setup — Stolpersteine

## Poetry statt pip

`pyproject.toml` ist die Quelle der Wahrheit, nicht `requirements.txt`
(existiert nicht). CI installiert über
`poetry install --with dev --no-root`.

## Drei getrennte Postgres-Instanzen in Prod/Staging

Anwendungs-DB, Terraform-State-DB und Keycloak-DB sind bewusst
getrennte Container (`postgres`, `postgres-tfstate`,
`keycloak-postgres`) — beim lokalen Debuggen eines DB-Problems zuerst
prüfen, gegen welche der drei man überhaupt verbunden ist.
`DATABASE_URL` zeigt nur auf die Anwendungs-DB.

## Security-Check läuft erst seit kurzem tatsächlich

Bis 2026-09-15 hatte GitHub Actions in der gesamten Org nie einen
Workflow-Run ausgeführt (Fork-Repos brauchen eine manuelle
"Enable workflows"-Bestätigung, die nie erfolgt war). Der
`pip-audit --strict`-Check im `🔒 Security`-Job war entsprechend nie
gelaufen — beim ersten echten Lauf kamen sofort bekannte CVEs in
`cryptography` und `gitpython` zutage. Nicht überrascht sein, wenn
`main` aktuell hinter dem Security-Gate hängt; das ist ein
nachgeholter Fund, keine neue Regression.
