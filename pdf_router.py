from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from celery.result import AsyncResult
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from backend import celery
from celery_app import translate_pdf as translate_pdf_task
from pdf_service import (
    DATA_DIR,
    MAX_UPLOAD_BYTES,
    UPLOAD_DIR,
    PdfParseError,
    ensure_pdf_dirs,
    parse_pdf_bytes,
    validate_pdf_bytes,
)
from translators import TRANSLATOR_REGISTRY

router = APIRouter(prefix="/pdf", tags=["pdf"])


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"detail": str(value)}


async def _read_pdf_upload(file: UploadFile) -> tuple[str, bytes]:
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="missing filename")
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="only .pdf files are accepted")

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large (limit {MAX_UPLOAD_BYTES} bytes)",
        )
    try:
        validate_pdf_bytes(data)
    except PdfParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return filename, data


@router.post("/parse", summary="upload and parse a PDF")
async def parse_pdf(file: UploadFile = File(...)):
    filename, data = await _read_pdf_upload(file)
    try:
        parsed = await run_in_threadpool(parse_pdf_bytes, data, filename)
    except PdfParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", **parsed}


@router.post("/translate", summary="upload a PDF and translate its text blocks")
async def translate_pdf(
    file: UploadFile = File(...),
    translate_model: str = Form(...),
    language: str = Form("zh"),
    model: str = Form(""),
    ignore_cache: bool = Form(False),
):
    if translate_model not in TRANSLATOR_REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=f"unknown engine {translate_model!r}, "
            f"available: {sorted(TRANSLATOR_REGISTRY)}",
        )

    filename, data = await _read_pdf_upload(file)
    ensure_pdf_dirs()
    job_id = uuid4().hex
    upload_path = (UPLOAD_DIR / f"{job_id}.pdf").resolve()
    try:
        upload_path.write_bytes(data)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"could not store upload: {exc}") from exc

    try:
        task = translate_pdf_task.delay(
            str(upload_path),
            language,
            translate_model,
            model,
            job_id,
            filename,
            ignore_cache,
        )
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise

    return {
        "status": "PENDING",
        "task_id": task.id,
        "job_id": job_id,
        "filename": filename,
        "size_bytes": len(data),
        "translate_model": translate_model,
        "language": language,
    }


@router.get("/result/{task_id}", summary="get PDF translation task result")
async def get_pdf_result(task_id: str):
    result = AsyncResult(id=task_id, app=celery)
    state = result.state
    if state == "FAILURE":
        return {"status": "FAILURE", "error": str(result.info)}
    if state != "SUCCESS":
        return {"status": state, "info": _as_dict(result.info)}

    data = _as_dict(result.result)
    return {
        "status": "SUCCESS",
        "task_id": task_id,
        "data": data,
        "download_url": f"/pdf/download/{task_id}?format=json",
        "mono_url": f"/pdf/download/{task_id}?format=mono",
        "dual_url": f"/pdf/download/{task_id}?format=dual",
    }


@router.get(
    "/download/{task_id}",
    summary="download translated PDF or per-block JSON",
)
async def download_pdf_result(task_id: str, format: str = "json"):
    if format not in ("json", "mono", "dual"):
        raise HTTPException(
            status_code=400,
            detail="format must be one of: json, mono, dual",
        )

    result = AsyncResult(id=task_id, app=celery)
    if not result.ready():
        raise HTTPException(status_code=409, detail="task is not finished")
    if result.state == "FAILURE":
        raise HTTPException(status_code=500, detail=str(result.info))

    data = _as_dict(result.result)
    if format == "json":
        raw_path = data.get("result_path")
        media_type = "application/json"
        download_name = f"{task_id}_pdf_translation.json"
    else:
        raw_path = data.get(f"{format}_path")
        media_type = "application/pdf"
        download_name = f"{data.get('job_id', task_id)}_{format}.pdf"

    if not raw_path:
        raise HTTPException(status_code=404, detail="result file not found")

    result_path = Path(str(raw_path)).resolve()
    data_dir = DATA_DIR.resolve()
    try:
        result_path.relative_to(data_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid result path") from exc
    if not result_path.is_file():
        raise HTTPException(status_code=404, detail="result file not found")

    return FileResponse(
        result_path,
        media_type=media_type,
        filename=download_name,
    )
