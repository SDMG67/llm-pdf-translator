import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from schema import Translate_Cache


def build_cache_key(original_text: str, params: dict) -> str:
    """Build a stable cache key from text + all quality-affecting params."""
    payload = {"text": original_text, "params": params}
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- Async API (used by FastAPI endpoints) ----------
async def get_cache(db: AsyncSession, cache_key: str) -> Translate_Cache | None:
    stmt = select(Translate_Cache).where(Translate_Cache.cache_key == cache_key)
    result = await db.execute(stmt)
    return result.scalars().first()


def get_cache_sync(db: Session, cache_key: str) -> Translate_Cache | None:
    """Synchronous cache lookup for the Celery worker."""
    stmt = select(Translate_Cache).where(Translate_Cache.cache_key == cache_key)
    result = db.execute(stmt)
    return result.scalars().first()


async def set_cache(
    db: AsyncSession, cache_key: str, original_text: str, translated_text: str
) -> Translate_Cache:
    existing = await get_cache(db, cache_key)
    if existing:
        existing.translated_text = translated_text
        await db.commit()
        await db.refresh(existing)
        return existing
    obj = Translate_Cache(
        cache_key=cache_key,
        original_text=original_text,
        translated_text=translated_text,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


# ---------- Sync API (used by synchronous Celery worker) ----------
def set_cache_sync(
    db: Session, cache_key: str, original_text: str, translated_text: str
) -> Translate_Cache:
    existing = db.execute(
        select(Translate_Cache).where(Translate_Cache.cache_key == cache_key)
    ).scalars().first()
    if existing:
        existing.translated_text = translated_text
        db.commit()
        db.refresh(existing)
        return existing
    obj = Translate_Cache(
        cache_key=cache_key,
        original_text=original_text,
        translated_text=translated_text,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj
