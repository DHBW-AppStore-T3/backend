# Häufige Fehlerbilder

| Symptom | Ursache | Lösung |
|---|---|---|
| Start bricht mit `unsafe production configuration: …` ab | `APP_ENV` ist `production` (Standard) mit Testschlüssel, `http`-Origin o. Ä. | Werte korrigieren; lokal `APP_ENV=development` (setzt `docker-compose.dev.yml`) |
| Start bricht mit `CREDENTIAL_ENCRYPTION_KEY …` ab | Schlüssel fehlt, ist kein url-safe Base64 oder nicht 32 Byte | `make env` erzeugt einen; sonst `python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` |
| API zeigt keine Outputs eines Tasks | Worker und API haben verschiedene `RESULTS_ENCRYPTION_KEY` | gleichen Schlüssel setzen (leer: beide fallen auf den Credential-Schlüssel zurück; `CREDENTIAL_ENCRYPTION_KEY` muss dann gleich sein) |
| Task bleibt PENDING | Worker läuft nicht, falsche Queue, oder Rolle `appstore_worker` ohne Login | `make dev-logs-worker`; `make migrate-dev` (setzt das Login und startet den Worker neu) |
| Task `failed` mit `worker_lost` | Lease abgelaufen, Worker gestorben | Destroy oder erneuter Deploy aus der UI; es gibt keinen automatischen Neustart |
| Task `failed`, `infrastructure` | Der Worker kennt den Job nicht (API und Worker nicht im Takt) | beide auf denselben Stand bringen, erneut deployen |
| 401 `Dev user '…' not found` | `X-Dev-User` ohne Seed-User | `make seed-dev` |
| 401 trotz gültigem Login | `iss`/`azp`/`aud` passen nicht | `KEYCLOAK_ISSUER` (öffentliche URL), `OIDC_ALLOWED_CLIENTS`, `OIDC_AUDIENCE` prüfen |
| 503 bei jeder Anfrage mit Token | Keycloak nicht erreichbar (`KEYCLOAK_SERVER_URL`) | Keycloak-Logs, `make health` |
| 409 `version_moved` | Tag der Version zeigt auf einen anderen Commit als freigegeben | Version neu einreichen |
| Upload 503 | `CLAMD_HOST` gesetzt, Daemon nicht erreichbar | ClamAV prüfen oder `CLAMD_HOST` leeren |
| Tests hängen oder erschöpfen den Pool | `DISABLE_BACKGROUND_TASKS` nicht gesetzt | `tests/conftest.py` setzt es; bei eigenen Skripten selbst setzen |
| Test sieht Zeile nicht (`visible_after <= now()`) | `now()` ist in Postgres der Transaktionsbeginn | in langlebiger Session vorher `rollback()` |
| Sammlung bricht ab: `tests without a 'unit' or 'integration' marker` | Testdatei ohne Marker | `pytestmark = pytest.mark.unit` oder `integration` |
| `Dev-Daten weg nach pytest` | Tests liefen ohne `TEST_DATABASE_URL` gegen die Dev-DB | siehe `README.md`, Abschnitt Tests |
