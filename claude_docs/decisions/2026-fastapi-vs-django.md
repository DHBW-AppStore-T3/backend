# FastAPI statt Django/Flask

FastAPI + Pydantic + SQLAlchemy 2.0: automatische OpenAPI-Dokumentation (Quelle des API-Vertrags, `scripts/export_openapi.py`), strikte Eingabevalidierung durch Pydantic, ORM mit Zugriff auf Raw-SQL für Postgres-Funktionen (Advisory Locks, `LISTEN/NOTIFY`, `SKIP LOCKED`).

Konsequenz für neuen Code: Handler sind synchron (`def`) und laufen im Threadpool; ein blockierender Aufruf in einem `async def`-Handler blockiert dagegen den Event-Loop. `async def` nur dort, wo gestreamt oder `await`et wird (SSE in `deployments.py`).
