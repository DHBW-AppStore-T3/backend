from celery import Celery
from kombu.transport import TRANSPORT_ALIASES

from .config import settings

# The broker is our own Postgres (.github#5): ``pgq+postgresql://…`` resolves
# to the transport in app/pgq.py. Registered before the app connects.
TRANSPORT_ALIASES.setdefault("pgq", "app.pgq:Transport")

celery_app = Celery("worker", broker=settings.celery_broker_url)  # no include here

celery_app.conf.update(
    broker_url=settings.celery_broker_url,
    broker_connection_retry_on_startup=True,
    # Each pooled producer holds one database connection.
    broker_pool_limit=2,

    task_serializer="json",
    accept_content=["json"],

    # No result backend: the worker writes status, logs and the sealed
    # outputs into the task row itself.
    task_ignore_result=True,
    # Celery events and remote control need a fanout exchange, which the
    # pgq transport does not have. Live events go through ``task_events``,
    # cancel through ``tasks.cancel_requested_at`` + NOTIFY.
    task_send_sent_event=False,
    worker_send_task_events=False,
    worker_enable_remote_control=False,
)

if __name__ == "__main__":
    celery_app.start()
