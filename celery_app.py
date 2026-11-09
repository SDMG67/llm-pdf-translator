import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from backend import celery
from database import SyncSessionLocal
from pdf_rebuild import rebuild_pdf
from pdf_service import OUTPUT_DIR, RESULT_DIR, ensure_pdf_dirs, parse_pdf_bytes
from translators import get_translator
from translators.batch import chunk_indices
from translators.errors import TranslationServiceUnavailable
import cache as cache_mod

logger = logging.getLogger(__name__)
PDF_BATCH_SIZE = 16
FREE_MODEL_BATCH_SIZE = 6
FREE_MODEL_MAX_CHARS = 1500
FREE_MODEL_WORKERS = 4
FREE_MODELS = {"bing", "google"}


def _save_cache(engine, original_text: str, translated_text: str) -> None:
    """Write cache using a SYNC session (worker runs outside an event loop)."""
    cache_key = cache_mod.build_cache_key(original_text, engine.cache_params())
    with SyncSessionLocal() as db:
        cache_mod.set_cache_sync(db, cache_key, original_text, translated_text)


def _merge_usage(total: dict, usage) -> None:
    if not usage:
        return
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value


@celery.task(bind=True)
def translate(self, original_text: str, language: str, translate_model: str):
    engine = get_translator(translate_model, lang_in="auto", lang_out=language)
    # Real translation call; any raised exception marks the task as FAILURE.
    translated_text = engine.translate(original_text)

    # Persist cache; never fail the whole task just because cache write failed.
    try:
        _save_cache(engine, original_text, translated_text)
    except Exception as exc:
        logger.warning("[cache] write failed: %s", exc)

    return {
        "translate_model": translate_model,
        "original_text": original_text,
        "language": language,
        "translated_text": translated_text,
        "usage": getattr(engine, "last_usage", None),
    }


@celery.task(bind=True)
def translate_pdf(
    self,
    pdf_path: str,
    language: str,
    translate_model: str,
    model: str = "",
    job_id: str = "",
    filename: str = "",
    ignore_cache: bool = False,
):
    """Translate a PDF in batches and rebuild mono/dual output files."""
    engine = get_translator(
        translate_model, lang_in="auto", lang_out=language, model=model
    )
    ensure_pdf_dirs()
    parsed = parse_pdf_bytes(
        Path(pdf_path).read_bytes(),
        filename=filename or Path(pdf_path).name,
    )

    source_blocks = parsed["blocks"]
    total = len(source_blocks)
    translated_texts: list[str | None] = [None] * total
    cache_hit_flags = [False] * total
    failed_flags = [False] * total
    pending: list[int] = []
    cache_hits = 0

    if not ignore_cache:
        with SyncSessionLocal() as db:
            for index, block in enumerate(source_blocks):
                cache_key = cache_mod.build_cache_key(
                    block["text"], engine.cache_params()
                )
                cached = cache_mod.get_cache_sync(db, cache_key)
                if cached is not None:
                    translated_texts[index] = cached.translated_text
                    cache_hit_flags[index] = True
                    cache_hits += 1

    for index, text in enumerate(translated_texts):
        if text is None:
            pending.append(index)

    processed = total - len(pending)
    self.update_state(
        state="PROGRESS",
        meta={
            "current": processed,
            "total": total,
            "percent": round(processed / total, 4) if total else 1.0,
            "cache_hits": cache_hits,
            "phase": "translating",
        },
    )

    errors: list[str] = []
    failed_blocks = 0
    usage_total: dict = {}

    def record_error(message: str) -> None:
        if len(errors) < 50:
            errors.append(message)

    def apply_batch(indexes, texts, batch_results) -> None:
        nonlocal processed, failed_blocks
        for index, original_text, translated_text in zip(
            indexes, texts, batch_results
        ):
            if translated_text is None or not str(translated_text).strip():
                translated_texts[index] = original_text
                failed_flags[index] = True
                failed_blocks += 1
                record_error(f"block {source_blocks[index]['id']} failed")
            else:
                translated_texts[index] = str(translated_text).strip()
                try:
                    _save_cache(
                        engine,
                        original_text,
                        translated_texts[index],
                    )
                except Exception as exc:
                    logger.warning("[cache] PDF block cache write failed: %s", exc)
                _merge_usage(usage_total, getattr(engine, "last_usage", None))
            processed += 1

        self.update_state(
            state="PROGRESS",
            meta={
                "current": processed,
                "total": total,
                "percent": round(processed / total, 4) if total else 1.0,
                "cache_hits": cache_hits,
                "failed_blocks": failed_blocks,
                "phase": "translating",
            },
        )

    def fallback_batch(texts):
        results = []
        for text in texts:
            try:
                results.append(engine.translate(text))
            except Exception as item_exc:
                results.append(None)
                record_error(f"item: {type(item_exc).__name__}: {item_exc}")
        return results

    pending_texts = [source_blocks[index]["text"] for index in pending]
    if translate_model in FREE_MODELS and pending:
        batches = [
            [pending[local] for local in group]
            for group in chunk_indices(
                pending_texts,
                max_items=FREE_MODEL_BATCH_SIZE,
                max_chars=FREE_MODEL_MAX_CHARS,
            )
        ]
        with ThreadPoolExecutor(max_workers=FREE_MODEL_WORKERS) as executor:
            futures = {}
            for indexes in batches:
                texts = [source_blocks[index]["text"] for index in indexes]
                futures[executor.submit(engine.translate_batch, texts)] = (
                    indexes,
                    texts,
                )

            for future in as_completed(futures):
                indexes, texts = futures[future]
                try:
                    batch_results = future.result()
                    if len(batch_results) != len(texts):
                        raise RuntimeError(
                            "batch translator returned wrong result count"
                        )
                except TranslationServiceUnavailable:
                    raise
                except Exception as exc:
                    logger.warning("[pdf] batch translation failed: %s", exc)
                    record_error(f"batch: {type(exc).__name__}: {exc}")
                    batch_results = fallback_batch(texts)
                apply_batch(indexes, texts, batch_results)
    else:
        for offset in range(0, len(pending), PDF_BATCH_SIZE):
            indexes = pending[offset : offset + PDF_BATCH_SIZE]
            texts = [source_blocks[index]["text"] for index in indexes]
            try:
                batch_results = engine.translate_batch(texts)
                if len(batch_results) != len(texts):
                    raise RuntimeError(
                        "batch translator returned wrong result count"
                    )
            except Exception as exc:
                logger.warning("[pdf] batch translation failed: %s", exc)
                record_error(f"batch: {type(exc).__name__}: {exc}")
                batch_results = fallback_batch(texts)
            apply_batch(indexes, texts, batch_results)

    if total and failed_blocks == total:
        raise RuntimeError(
            "PDF translation failed for all blocks: " + "; ".join(errors[:5])
        )

    translated_blocks = []
    for index, block in enumerate(source_blocks):
        translated_blocks.append(
            {
                "id": block["id"],
                "page": block["page"],
                "bbox": block["bbox"],
                "original_text": block["text"],
                "translated_text": translated_texts[index],
                "cache_hit": cache_hit_flags[index],
                "failed": failed_flags[index],
                "font_size": block.get("font_size"),
                "bold": bool(block.get("bold", False)),
                "italic": bool(block.get("italic", False)),
                "font": block.get("font", ""),
            }
        )

    by_id = {block["id"]: block for block in translated_blocks}
    pages = [
        {
            "page": page["page"],
            "width": page["width"],
            "height": page["height"],
            "blocks": [
                by_id[block["id"]]
                for block in page["blocks"]
                if block["id"] in by_id
            ],
        }
        for page in parsed["pages"]
    ]

    message = None
    if total == 0:
        message = "no extractable text (possibly a scanned PDF; OCR is not supported yet)"
    elif failed_blocks:
        message = (
            f"{failed_blocks} block(s) could not be translated; source text was kept"
        )

    result = {
        "job_type": "pdf",
        "job_id": job_id or self.request.id,
        "filename": parsed["filename"],
        "page_count": parsed["page_count"],
        "parsed_pages": parsed["parsed_pages"],
        "block_count": total,
        "translated_block_count": total - failed_blocks,
        "cache_hits": cache_hits,
        "failed_blocks": failed_blocks,
        "has_text": parsed["has_text"],
        "truncated": parsed["truncated"],
        "translate_model": translate_model,
        "model": engine.model,
        "language": language,
        "usage": usage_total or None,
        "message": message,
        "errors": errors,
        "failed_block_ids": [
            block["id"]
            for block in translated_blocks
            if block["failed"]
        ],
        "pages": pages,
        "blocks": translated_blocks,
    }

    result_path = (RESULT_DIR / f"{result['job_id']}.json").resolve()
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    self.update_state(
        state="PROGRESS",
        meta={
            "current": total,
            "total": total,
            "percent": 0.99,
            "cache_hits": cache_hits,
            "failed_blocks": failed_blocks,
            "phase": "rebuilding",
        },
    )

    mono_path = (OUTPUT_DIR / f"{result['job_id']}_mono.pdf").resolve()
    dual_path = (OUTPUT_DIR / f"{result['job_id']}_dual.pdf").resolve()
    try:
        rebuilt = rebuild_pdf(pdf_path, pages, mono_path, dual_path, language)
    except Exception:
        logger.exception("[pdf] layout rebuild failed")
        raise

    summary_keys = (
        "job_type",
        "job_id",
        "filename",
        "page_count",
        "parsed_pages",
        "block_count",
        "translated_block_count",
        "cache_hits",
        "failed_blocks",
        "has_text",
        "truncated",
        "translate_model",
        "model",
        "language",
        "usage",
        "message",
    )
    summary = {key: result[key] for key in summary_keys}
    summary["result_path"] = str(result_path)
    summary["mono_path"] = rebuilt["mono_path"]
    summary["dual_path"] = rebuilt["dual_path"]
    return summary
