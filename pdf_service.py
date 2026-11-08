"""PDF upload validation and text-block extraction.

PyMuPDF is imported lazily so the rest of the API can start even when the
optional PDF dependency is missing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PDF_MAGIC = b"%PDF-"
MAX_UPLOAD_BYTES = int(os.environ.get("PDF_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_PAGES = int(os.environ.get("PDF_MAX_PAGES", "200"))
MAX_BLOCKS = int(os.environ.get("PDF_MAX_BLOCKS", "5000"))
MAX_TEXT_CHARS = int(os.environ.get("PDF_MAX_TEXT_CHARS", "500000"))

DATA_DIR = Path(os.environ.get("PDF_DATA_DIR", "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
RESULT_DIR = DATA_DIR / "results"
OUTPUT_DIR = DATA_DIR / "outputs"


class PdfParseError(ValueError):
    """A user-facing PDF validation/parsing failure."""


def ensure_pdf_dirs() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def _pymupdf():
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - exercised when dep missing
        raise PdfParseError(
            "PyMuPDF is not installed; run: pip install pymupdf"
        ) from exc
    return pymupdf


def validate_pdf_bytes(data: bytes) -> None:
    if not data:
        raise PdfParseError("empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise PdfParseError(
            f"file too large: {len(data)} bytes (limit {MAX_UPLOAD_BYTES})"
        )
    # Some PDFs have a small prefix before %PDF-; inspect a bounded header.
    if PDF_MAGIC not in data[:1024]:
        raise PdfParseError("not a PDF file (missing %PDF- header)")


def _clean_metadata(metadata: Any) -> dict[str, str | None]:
    if not isinstance(metadata, dict):
        return {}
    return {
        str(key): (str(value) if value is not None else None)
        for key, value in metadata.items()
    }


def extract_span_styles(page) -> list[dict[str, Any]]:
    """Collect original span sizes/flags so headings and bold text survive."""

    styles: list[dict[str, Any]] = []
    data = page.get_text("dict")
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue
                styles.append(
                    {
                        "bbox": [float(value) for value in span["bbox"]],
                        "size": float(span.get("size", 0.0)),
                        "flags": int(span.get("flags", 0)),
                        "font": str(span.get("font", "")),
                        "text": text,
                    }
                )
    return styles


def style_for_bbox(
    span_styles: list[dict[str, Any]],
    bbox: list[float],
) -> dict[str, Any]:
    """Match spans to a text block and derive its dominant style."""

    x0, y0, x1, y1 = (float(value) for value in bbox)
    matched: list[dict[str, Any]] = []
    for span in span_styles:
        sx0, sy0, sx1, sy1 = span["bbox"]
        center_x = (sx0 + sx1) / 2.0
        center_y = (sy0 + sy1) / 2.0
        if (
            x0 - 2.0 <= center_x <= x1 + 2.0
            and y0 - 2.0 <= center_y <= y1 + 2.0
        ):
            matched.append(span)
    if not matched:
        return {}

    total_chars = sum(max(1, len(span["text"].strip())) for span in matched)
    bold_chars = sum(
        max(1, len(span["text"].strip()))
        for span in matched
        if span["flags"] & 16
    )
    italic_chars = sum(
        max(1, len(span["text"].strip()))
        for span in matched
        if span["flags"] & 2
    )
    significant_sizes = [
        span["size"]
        for span in matched
        if len(span["text"].strip()) >= 2
    ]
    font_size = max(significant_sizes or [span["size"] for span in matched])

    font_chars: dict[str, int] = {}
    for span in matched:
        font_chars[span["font"]] = font_chars.get(span["font"], 0) + max(
            1, len(span["text"].strip())
        )
    font = max(font_chars, key=font_chars.get) if font_chars else ""

    return {
        "font_size": round(float(font_size), 2),
        "bold": bold_chars * 2 >= total_chars,
        "italic": italic_chars * 2 >= total_chars,
        "font": font,
    }


def parse_pdf_bytes(
    data: bytes,
    filename: str = "upload.pdf",
    max_pages: int | None = None,
    max_blocks: int | None = None,
) -> dict[str, Any]:
    """Extract page text and positioned text blocks from a PDF.

    The result is intentionally JSON-friendly so it can be returned by the API
    and stored by the Celery worker.
    """

    validate_pdf_bytes(data)
    pymupdf = _pymupdf()
    page_limit = max_pages if max_pages is not None else MAX_PAGES
    block_limit = max_blocks if max_blocks is not None else MAX_BLOCKS

    document = None
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
        if getattr(document, "needs_pass", False):
            raise PdfParseError("password-protected PDFs are not supported")

        page_count = int(document.page_count)
        if page_count == 0:
            raise PdfParseError("PDF has no pages")

        parsed_page_count = min(page_count, max(page_limit, 0))
        truncated_pages = page_count > parsed_page_count
        truncated_blocks = False
        pages: list[dict[str, Any]] = []
        blocks: list[dict[str, Any]] = []
        page_texts: list[str] = []

        for page_index in range(parsed_page_count):
            page = document[page_index]
            span_styles = extract_span_styles(page)
            raw_blocks = page.get_text("blocks", sort=True) or []
            text_blocks: list[tuple[float, float, float, float, str, int]] = []

            for raw in raw_blocks:
                if len(raw) < 7 or int(raw[6]) != 0:
                    continue
                x0, y0, x1, y1, text, block_no = raw[:6]
                text = str(text).strip()
                if text:
                    text_blocks.append(
                        (float(x0), float(y0), float(x1), float(y1), text, int(block_no))
                    )

            # A few unusual PDFs expose text through get_text("text") only.
            if not text_blocks:
                fallback = (page.get_text("text") or "").strip()
                if fallback:
                    rect = page.rect
                    text_blocks.append(
                        (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1), fallback, 0)
                    )

            page_blocks: list[dict[str, Any]] = []
            for block_index, (x0, y0, x1, y1, text, block_no) in enumerate(
                text_blocks, start=1
            ):
                if len(blocks) >= block_limit:
                    truncated_blocks = True
                    break
                block = {
                    "id": f"p{page_index + 1}-b{block_index}",
                    "page": page_index + 1,
                    "bbox": [
                        round(x0, 2),
                        round(y0, 2),
                        round(x1, 2),
                        round(y1, 2),
                    ],
                    "block_no": block_no,
                    "text": text,
                }
                block.update(
                    style_for_bbox(span_styles, [x0, y0, x1, y1])
                )
                blocks.append(block)
                page_blocks.append(block)

            page_text = "\n".join(block["text"] for block in page_blocks).strip()
            page_texts.append(page_text)
            pages.append(
                {
                    "page": page_index + 1,
                    "width": round(float(page.rect.width), 2),
                    "height": round(float(page.rect.height), 2),
                    "block_count": len(page_blocks),
                    "text": page_text,
                    "blocks": page_blocks,
                }
            )
            if truncated_blocks:
                break

        full_text = "\n\n".join(text for text in page_texts if text)
        text_truncated = len(full_text) > MAX_TEXT_CHARS
        if text_truncated:
            full_text = full_text[:MAX_TEXT_CHARS]

        return {
            "filename": Path(filename).name or "upload.pdf",
            "size_bytes": len(data),
            "page_count": page_count,
            "parsed_pages": len(pages),
            "block_count": len(blocks),
            "char_count": len(full_text),
            "has_text": bool(blocks),
            # `truncated` means the translation block set is incomplete.
            # Capping the text preview does not drop any translatable block.
            "truncated": truncated_pages or truncated_blocks,
            "truncated_pages": truncated_pages,
            "truncated_blocks": truncated_blocks,
            "text_truncated": text_truncated,
            "text_preview_truncated": text_truncated,
            "parser": f"pymupdf/{getattr(pymupdf, '__version__', 'unknown')}",
            "metadata": _clean_metadata(getattr(document, "metadata", None)),
            "pages": pages,
            "blocks": blocks,
            "text": full_text,
        }
    except PdfParseError:
        raise
    except Exception as exc:
        raise PdfParseError(f"failed to parse PDF: {exc}") from exc
    finally:
        if document is not None:
            try:
                document.close()
            except Exception:
                pass
