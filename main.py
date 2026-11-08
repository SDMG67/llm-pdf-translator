import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import init_db, get_db
from pdf_service import ensure_pdf_dirs
import pdf_router
from schema import Translate, TranslatorRequest
import translator


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_pdf_dirs()
    await init_db()
    yield


app = FastAPI(
    title="LLM Translator API",
    version="0.2.0",
    lifespan=lifespan,
)

# CORS: allow the frontend (Gradio/browser) to call this API cross-origin
_origins = os.environ.get("CORS_ORIGINS", "*").split(",")
_origin_list = [o.strip() for o in _origins if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origin_list,
    # Browsers reject allow_origins=["*"] together with credentials.
    allow_credentials="*" not in _origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(translator.translator)
app.include_router(pdf_router.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"status": "ERROR", "detail": str(exc)},
    )


@app.get("/health", summary="health check")
async def health():
    return {"status": "ok"}


@app.post("/cache", summary="add")
async def create_cache(request: TranslatorRequest, db: AsyncSession = Depends(get_db)):
    add = Translate(**request.model_dump())
    db.add(add)
    await db.commit()
    return request


@app.delete("/cache/{id}", summary="delete")
async def delete_cache(id: int, db: AsyncSession = Depends(get_db)):
    result = await db.get(Translate, id)
    if result is None:
        raise HTTPException(status_code=404, detail="Not Found")
    await db.delete(result)
    await db.commit()
    return {"deleted": id}


@app.get("/cache", summary="list")
async def list_cache(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Translate))
    answer = result.scalars().all()
    return [
        {
            "id": item.id,
            "translate_model": item.translate_model,
            "language": item.language,
            "original_text": item.original_text,
        }
        for item in answer
    ]
