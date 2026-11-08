from fastapi import APIRouter, Depends, HTTPException
from schema import TranslatorRequest
from database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
import cache
from backend import celery
from celery_app import translate as translate_task
from celery.result import AsyncResult
from translators import TRANSLATOR_REGISTRY, get_translator

translator = APIRouter(tags=["translator"], prefix="/translator")


def _as_dict(value) -> dict:
    """Normalize Celery info/result (may be exception, str or dict) into a dict."""
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"detail": str(value)}


@translator.post("/create", summary="create translation task")
async def create_translate_task(
    request: TranslatorRequest, db: AsyncSession = Depends(get_db)
):
    # 1) validate engine first (cheap, gives clear 400 before any cache work)
    if request.translate_model not in TRANSLATOR_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"unknown engine {request.translate_model!r}, "
            f"available: {sorted(TRANSLATOR_REGISTRY)}",
        )

    # 2) build engine instance so the cache key reflects quality params
    #    (model, temperature, ...) in addition to text and language.
    engine = get_translator(
        request.translate_model, lang_in="auto", lang_out=request.language
    )
    cache_key = cache.build_cache_key(
        request.original_text, engine.cache_params()
    )

    cached = await cache.get_cache(db, cache_key)
    if cached is not None:
        return {
            "status": "SUCCESS",
            "cached": True,
            "data": {
                "translate_model": request.translate_model,
                "original_text": request.original_text,
                "language": request.language,
                "translated_text": cached.translated_text,
            },
        }

    # 3) enqueue task; the worker writes the cache when the task completes.
    task = translate_task.delay(
        request.original_text, request.language, request.translate_model
    )
    return {"status": "PENDING", "cached": False, "task_id": task.id}


@translator.get("/get/{task_id}", summary="get translation result")
async def get_translate_result(task_id: str, db: AsyncSession = Depends(get_db)):
    result = AsyncResult(id=task_id, app=celery)
    state = result.state  # PENDING / STARTED / PROGRESS / SUCCESS / FAILURE / REVOKED

    if state == "FAILURE":
        return {"status": "FAILURE", "error": str(result.info)}
    if state == "REVOKED":
        return {"status": "REVOKED"}
    if state != "SUCCESS":
        return {"status": state, "info": _as_dict(result.info)}

    data = _as_dict(result.result)
    return {"status": "SUCCESS", "cached": False, "data": data}


@translator.delete("/{task_id}", summary="cancel task")
async def cancel_translate_task(task_id: str):
    result = AsyncResult(id=task_id, app=celery)
    # terminate=True forcibly ends the running worker process.
    result.revoke(terminate=True)
    return {"status": "REVOKED", "task_id": task_id}