# Authentifizierung & Rollen

## Anmeldung

Der Browser meldet sich bei Keycloak an (Realm `dhbw`, Client `appstore-frontend`, PKCE S256). Das Backend prüft nur Access-Tokens (`app/utils/oidc.py`, `app/utils/keycloak_auth.py`).

Reihenfolge in `get_current_user_keycloak`:

1. `APP_ENV=development` und Header `X-Dev-User: <E-Mail>`: der Seed-User mit dieser E-Mail, ohne Token (`make seed-dev` legt `prof@dhbw.de`, `student@dhbw.de`, `admin@dhbw.de` an). In Produktion nicht verfügbar.
2. Kein `Authorization: Bearer`: 401.
3. LTI-Session-Token (HS256, von `/lti13/session` ausgestellt, nur wenn `LTI13_SESSION_SECRET` gesetzt ist).
4. Keycloak-Access-Token, geprüft mit PyJWT:
   - Signatur gegen das JWKS des Realms (intern über `KEYCLOAK_SERVER_URL`), nur asymmetrische Algorithmen, Schlüssel eine Stunde gecacht, bei unbekannter `kid` neu geladen;
   - `iss` = `KEYCLOAK_ISSUER`;
   - `azp` ∈ `OIDC_ALLOWED_CLIENTS`; mit `OIDC_AUDIENCE` muss `aud` ihn enthalten; `typ` = `Bearer`;
   - Keycloak nicht erreichbar: 503, ungültiges Token: 401.
5. Beim ersten Token legt `sync_user_from_keycloak` den User an (JIT) und aktualisiert Name, E-Mail und `keycloak_role`.

## Rollen

`student` < `teacher` < `admin`. `users.role` ist die höchste von drei Aussagen (`utils/roles.py`), jede Quelle überschreibt nur ihren Wert:

| Spalte | Quelle |
|---|---|
| `keycloak_role` | Realm-Rollen des letzten Tokens (`admin` > `teacher` > sonst student) |
| `lti_role` | letzter LTI-Start (Instructor = teacher, sonst student, nie admin) |
| `granted_role` | Vergabe durch einen Admin (`PUT /users/{id}`) |

Rollenprüfungen: `utils/capabilities.py` (`can_*`), `utils/permissions.py` (`ensure_*`), Zugriff auf Zugangsdaten `services/access.py`.

## LTI 1.3 (Moodle)

`GET|POST /lti13/login` (OIDC-Initiierung), `POST /lti13/launch` (validiert `id_token` gegen `LTI13_PLATFORM_JWKS_URL`, `iss`/`client_id`/`deployment_id`, Nonce in `lti_nonces`), `POST /lti13/session` (löst den Einmal-Code aus `lti_login_codes` gegen eine Session ein), `GET /lti13/jwks`. Identität: `users.lti_issuer` + `lti_subject`; Kurs: `courses.lti_issuer` + `lti_context_id`. Cookies `lti13_state`/`lti13_binding` heißen in Produktion `__Host-…` mit `Secure`, in Entwicklung ohne Präfix.

## Schutz gegen fremde Seiten

`CrossSiteWriteGuard` (`utils/cross_site.py`) verweigert schreibende Browser-Requests, deren Origin nicht in `APP_BASE_URL`, `CORS_ORIGINS` oder `CSRF_TRUSTED_ORIGINS` steht.

## Produktions-Start

`APP_ENV=production` (Standard) bricht den Start ab bei: veröffentlichtem Fernet-Testschlüssel, `RESULTS_ENCRYPTION_KEY` = Credential-Schlüssel, `KEYCLOAK_ISSUER` ohne `https://`, leerem `OIDC_ALLOWED_CLIENTS`, nicht-https `CORS_ORIGINS`, LTI-Secret unter 32 Zeichen bei gesetzter `LTI13_CLIENT_ID`.
