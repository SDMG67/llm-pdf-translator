import os

from celery import Celery
from dotenv import load_dotenv

DEFAULT_BROKER = "sqla+sqlite:///./celery_broker.db"
DEFAULT_BACKEND = "db+sqlite:///./celery_results.db"

load_dotenv()

broker_url = os.environ.get("CELERY_BROKER", DEFAULT_BROKER)
result_backend = os.environ.get("CELERY_RESULT", DEFAULT_BACKEND)

celery = Celery(
    "tasks",
    broker=broker_url,
    backend=result_backend,
    include=["celery_app"],  # only include the task module; backend.py is the app config itself
)

celery.conf.update(
    worker_pool="solo",
    task_track_started=True,          # show STARTED while running instead of stuck on PENDING
    result_extended=True,             # keep extra metadata in results
    broker_connection_retry_on_startup=True,
)