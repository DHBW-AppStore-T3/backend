# API-Verträge mit frontend und worker

## frontend → backend

Alles unter `/api/` (nginx strippt das Präfix, siehe
`deployment/nginx/nginx.conf`). Kein OpenAPI-Contract-File gepflegt —
FastAPI generiert `/docs` automatisch, das ist die Quelle der Wahrheit
für Endpunkt-Signaturen. Live-Updates (Deployment-Fortschritt) laufen
nicht über Polling, sondern Server-Sent-Events aus
`deployment_pubsub.py`.

## backend → worker (nicht direkt — über Celery)

Backend published Tasks über `CELERY_BROKER_URL` (RabbitMQ), der
Worker konsumiert sie. Kein direkter HTTP-Call zwischen den beiden
Services. Der Task-Name und das Payload-Schema müssen zwischen
`backend/app/services/task_service.py` und dem entsprechenden
Celery-Task im `worker`-Repo synchron gehalten werden — bei einer
Signaturänderung auf einer Seite ohne die andere anzupassen, scheitert
der Task erst zur Laufzeit, nicht beim Build.

**Wo das im Worker-Repo nachzuschlagen ist:**
`worker/claude_docs/architecture/task-contracts.md`.

## worker → backend (Ergebnisse zurück)

Nicht direkt — der Worker schreibt Ergebnisse nach Redis
(`CELERY_RESULT_BACKEND`), `celery_event_listener.py` im Backend
konsumiert das. Für Live-Log-Streaming während eines Deploys nutzt der
Worker zusätzlich Redis Pub/Sub, das Backend leitet das per SSE ans
Frontend weiter (`deployment_pubsub.py`).

## Terraform-State

Der Worker schreibt Terraform-State in `postgres-tfstate` (eigene,
vom Backend isolierte DB). Das Backend liest daraus nur lesend über
`tf_state_parser.py`, um Infrastruktur-Details im UI anzuzeigen —
schreibt nie direkt in diese DB.
