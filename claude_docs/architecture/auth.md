# Keycloak-Integration

`app/utils/keycloak_auth.py`. Backend validiert RS256-JWTs, die
Keycloak ausstellt — der eigentliche Login-Flow läuft im Frontend
(Vue/Keycloak-JS), das Backend prüft nur Tokens.

## Konfiguration

`KEYCLOAK_SERVER_URL`, `KEYCLOAK_REALM` (Default `dhbw`),
`KEYCLOAK_CLIENT_ID` (Default `appstore-backend`),
`KEYCLOAK_CLIENT_SECRET`. In Prod: `KEYCLOAK_ENABLED` muss `true`
sein (Default), sonst läuft die App ohne Auth-Prüfung — nur für lokale
Entwicklung gedacht, nie in Staging/Prod deaktivieren.

## Token-Flow

1. Frontend holt Access-Token von Keycloak
2. Jeder Backend-Request trägt `Authorization: Bearer <token>`
3. `keycloak_auth.py` verifiziert die RS256-Signatur gegen Keycloaks
   öffentlichen Schlüssel, liest Claims (Rolle, User-ID)
4. Rollenprüfung gegen `UserRole`-Enum aus `models.py`

## Bekannte, absichtlich tolerierte Schwachstelle

`python-jose` (für JWT-Verifikation) zieht transitiv `ecdsa` mit einer
bekannten Timing-Seitenkanal-Lücke (PYSEC-2026-1325) — im
`pip-audit`-Security-Check explizit mit `--ignore-vuln` ausgenommen.
Begründung im CI-Workflow-Kommentar: die Lücke betrifft nur
*Signieren* (`sign_digest`), das Backend nutzt `python-jose`
ausschließlich zum *Verifizieren* eingehender Keycloak-Tokens — die
Angriffsfläche ist nicht vorhanden. Bei einem Wechsel weg von
`python-jose` (z. B. zu PyJWT) oder falls das Backend jemals selbst
JWTs signiert, muss diese Ausnahme neu bewertet werden.
