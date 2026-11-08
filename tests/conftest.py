"""Test fixtures: isolate DB + run Celery tasks eagerly (no broker needed).

Environment variables MUST be set before importing the app modules, since
`database.py` builds the engine at import time.
"""
import os
import tempfile

# --- Configure a throwaway SQLite DB and in-memory Celery BEFORE any app import.
_tmp_db = os.path.join(tempfile.gettempdir(), "pdf2zh_test.db")
if os.path.exists(_tmp_db):
    os.remove(_tmp_db)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp_db}"
os.environ["CELERY_BROKER"] = "memory://"
os.environ["CELERY_RESULT"] = "cache+memory://"
os.environ["PDF_DATA_DIR"] = tempfile.mkdtemp(prefix="pdf2zh_pdf_")

import pytest_asyncio  # noqa: E402
from httpx import AsyncClient, ASGITransport  # noqa: E402

import backend  # noqa: E402

# Run tasks synchronously inside the API process and persist their results
# so AsyncResult(...) lookups in /translator/get can find them.
backend.celery.conf.task_always_eager = True
backend.celery.conf.task_eager_propagates = True
backend.celery.conf.task_store_eager_result = True

from main import app  # noqa: E402
from database import init_db  # noqa: E402


@pytest_asyncio.fixture
async def client():
    await init_db()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
