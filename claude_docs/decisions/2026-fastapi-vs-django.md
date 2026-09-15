# FastAPI statt Django/Flask

Aus `technologiestack.md` (`.github`-Repo): FastAPI + Pydantic +
async/await statt klassischer WSGI-Frameworks. Begründung dort:
native Async-Unterstützung über Starlette, automatische
OpenAPI-Dokumentation (`/docs`), rigorose Eingabevalidierung durch
Pydantic. SQLAlchemy 2.0 wurde bewusst gewählt statt eines
simpleren ORMs, weil es sowohl objektorientierten Zugriff als auch
Raw-SQL erlaubt, wenn die ORM-Abstraktion für komplexe
OpenStack-Resource-Queries nicht ausreicht.

**Konsequenz für neuen Code:** Endpunkte async schreiben, keine
blockierenden I/O-Calls im Request-Handler ohne `await` — sonst
blockiert das den gesamten Event-Loop, nicht nur den einen Request.
