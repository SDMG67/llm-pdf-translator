import os

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker

from schema import Base

# Allow overriding the database via env var (used by tests and containers).
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./test.db")


# --- Async engine (used by the FastAPI app) ---
def _sync_url(async_url: str) -> str:
    """Derive a sync SQLAlchemy URL from the async one (worker side)."""
    return (
        async_url.replace("+aiosqlite", "")
        .replace("+asyncpg", "+psycopg2")
    )


_async_connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    _async_connect_args = {"check_same_thread": False}

async_engine = create_async_engine(
    url=DATABASE_URL,
    connect_args=_async_connect_args,
    pool_size=5,
    max_overflow=10,
    echo=False,
)

SessionLocal = async_sessionmaker(bind=async_engine, class_=AsyncSession)


# --- Sync engine (used by Celery worker, which is synchronous) ---
SYNC_DATABASE_URL = _sync_url(DATABASE_URL)
_sync_connect_args = {}
if SYNC_DATABASE_URL.startswith("sqlite"):
    _sync_connect_args = {"check_same_thread": False}

sync_engine = create_engine(
    url=SYNC_DATABASE_URL,
    connect_args=_sync_connect_args,
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False)


async def init_db():
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with SessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
