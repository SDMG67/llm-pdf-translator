"""Rebuild a PDF with translated text inside safe block regions."""

from __future__ import annotations

import re
import statistics
from pathlib import Path
from typing import Any

from pdf_service import extract_span_styles, style_for_bbox


def _pymupdf():
    import pymupdf

    return pymupdf


def font_for_language(language: str) -> str:
    """Pick a built-in CJK font for common target languages, else Helvetica."""

    lang = (language or "").lower()
    if lang.startswith("zh-tw") or lang.startswith("zh-hant"):
        return "china-t"
    if lang.startswith("zh"):
        return "china-s"
    if lang.startswith("ja"):
        return "japan"
    if lang.startswith("ko"):
        return "korea"
    return "helv"


def _layout_bboxes(page_rect, bboxes: list[list[float] | None]):
    """Clip two-column blocks to their column and all blocks to page margins."""

    margin = 24.0
    center = (float(page_rect.x0) + float(page_rect.x1)) / 2.0
    narrow = [
        bbox
        for bbox in bboxes
        if bbox and (bbox[2] - bbox[0]) < float(page_rect.width) * 0.6
    ]
    left_count = sum(1 for bbox in narrow if bbox[2] <= center - 2.0)
    right_count = sum(1 for bbox in narrow if bbox[0] >= center + 2.0)
    two_columns = left_count >= 2 and right_count >= 2

    result: list[list[float] | None] = []
    for bbox in bboxes:
        if not bbox:
            result.append(None)
            continue
        x0, y0, x1, y1 = (float(value) for value in bbox)
        full_width = (x1 - x0) > float(page_rect.width) * 0.6
        if not two_columns or full_width:
            x0 = max(x0, float(page_rect.x0) + margin)
            x1 = min(x1, float(page_rect.x1) - margin)
        elif (x0 + x1) / 2.0 < center:
            x0 = max(x0, float(page_rect.x0) + margin)
            x1 = min(x1, center - 6.0)
        else:
            x0 = max(x0, center + 6.0)
            x1 = min(x1, float(page_rect.x1) - margin)
        if x1 > x0:
            result.append([x0, y0, x1, y1])
        else:
            result.append(None)
    return result


def _safe_rect(
    page_rect,
    bbox: list[float],
    all_bboxes: list[list[float] | None],
    current_index: int,
    gap: float = 3.0,
    bottom_margin: float = 14.0,
):
    """Keep the source column width and grow only until the next block."""

    pymupdf = _pymupdf()
    x0, y0, x1, y1 = (float(value) for value in bbox)
    safe_bottom = float(page_rect.y1) - bottom_margin

    for index, other in enumerate(all_bboxes):
        if index == current_index or not other or len(other) != 4:
            continue
        ox0, oy0, ox1, _oy1 = (float(value) for value in other)
        overlaps_x = min(x1, ox1) > max(x0, ox0) + 1.0
        below = oy0 > y0 + 1.0
        if overlaps_x and below:
            safe_bottom = min(safe_bottom, oy0 - gap)

    # Never shrink below the source box; if there is no room, the text fitter
    # truncates instead of drawing over the next block.
    safe_bottom = max(y1, safe_bottom)
    return pymupdf.Rect(x0, y0, x1, safe_bottom)


_CJK_RANGES = (
    (0x2E80, 0x2FFF),
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0xFE30, 0xFE4F),
    (0xFF00, 0xFFEF),
    (0x20000, 0x2FA1F),
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in _CJK_RANGES)


def _font_set(pymupdf, fontname: str, bold: bool = False):
    latin = pymupdf.Font("hebo" if bold else "helv")
    cjk = None
    cjk_name = "helv"
    if fontname != "helv":
        try:
            cjk = pymupdf.Font(fontname)
            cjk_name = fontname
        except Exception:
            cjk = None
    return {
        "cjk": cjk,
        "latin": latin,
        "cjk_name": cjk_name,
        "latin_name": "hebo" if bold else "helv",
    }


def _font_parts(text: str, fonts) -> list[tuple[str, str]]:
    if not text:
        return []
    parts: list[tuple[str, str]] = []
    current_key = (
        "cjk"
        if fonts["cjk"] is not None and _is_cjk(text[0])
        else "latin"
    )
    current = text[0]
    for char in text[1:]:
        key = (
            "cjk"
            if fonts["cjk"] is not None and _is_cjk(char)
            else "latin"
        )
        if key == current_key:
            current += char
        else:
            parts.append((current_key, current))
            current_key = key
            current = char
    parts.append((current_key, current))
    return parts


def _rich_length(text: str, fonts, size: float) -> float:
    return sum(
        fonts[key].text_length(part, size)
        for key, part in _font_parts(text, fonts)
    )


def _atoms(text: str, fonts) -> list[str]:
    """Split a justified line at CJK character / Latin word boundaries."""

    pattern = (
        r"[\u2e80-\u2fff\u3000-\u303f\u3040-\u30ff"
        r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
        r"\ufe30-\ufe4f\uff00-\uffef]|\s+"
        r"|[^\s\u2e80-\u2fff\u3000-\u303f\u3040-\u30ff"
        r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
        r"\ufe30-\ufe4f\uff00-\uffef]+"
    )
    return re.findall(pattern, text)


def _wrap_text(text: str, fonts, size: float, width: float) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        current = ""
        for token in re.findall(r"\s+|\S+", paragraph):
            if token.isspace():
                if not current and not lines:
                    # Preserve the two-character first-line indent.
                    current = token
                elif current and not current.endswith(" "):
                    current += " "
                continue

            candidate = current + token
            if _rich_length(candidate, fonts, size) <= width:
                current = candidate
                continue

            if current.strip():
                lines.append(current.rstrip())
                current = ""

            if _rich_length(token, fonts, size) <= width:
                current = token
                continue

            # Long URLs / unbroken CJK runs: split by character.
            for char in token:
                candidate = current + char
                if current and _rich_length(candidate, fonts, size) > width:
                    lines.append(current.rstrip())
                    current = char
                else:
                    current = candidate
        if current or not lines:
            lines.append(current.rstrip())
    return lines or [""]


def _draw_lines(
    page,
    rect,
    lines: list[str],
    fonts,
    size: float,
    bold: bool = False,
    justify: bool = False,
    max_width: float | None = None,
) -> None:
    baseline = float(rect.y0) + size
    line_height = size * 1.18
    for line_index, line in enumerate(lines):
        x = float(rect.x0) + 0.5
        is_last_line = line_index == len(lines) - 1
        gap = 0.0
        atoms = None
        if (
            justify
            and not is_last_line
            and max_width
        ):
            possible_gap = max(
                0.0,
                (float(max_width) - _rich_length(line, fonts, size))
                / max(1, len(_atoms(line, fonts)) - 1),
            )
            # Only justify when the needed expansion is modest. This avoids
            # both ragged sparse lines and absurd gaps.
            if size * 0.12 <= possible_gap <= size * 0.6:
                gap = possible_gap
                atoms = _atoms(line, fonts)

        if atoms is None:
            atoms_to_draw = [line]
        else:
            atoms_to_draw = atoms

        for atom_index, atom in enumerate(atoms_to_draw):
            for key, part in _font_parts(atom, fonts):
                if part:
                    text_kwargs = {}
                    if bold and key == "cjk":
                        # Built-in CJK fonts have no bold face; a light stroke
                        # preserves the visual weight without changing metrics.
                        text_kwargs = {
                            "render_mode": 2,
                            "border_width": 0.02,
                        }
                    page.insert_text(
                        (x, baseline),
                        part,
                        fontsize=size,
                        fontname=fonts[f"{key}_name"],
                        color=(0, 0, 0),
                        **text_kwargs,
                    )
                    x += fonts[key].text_length(part, size)
            if gap and atom_index < len(atoms_to_draw) - 1:
                x += gap
        baseline += line_height


def _draw_fitted(
    page,
    rect,
    text: str,
    fontname: str,
    base_height: float | None = None,
    source_font_size: float | None = None,
    bold: bool = False,
    indent: bool = False,
    justify: bool = False,
) -> float:
    """Draw mixed CJK/Latin text with each script's real font metrics."""

    pymupdf = _pymupdf()
    fonts = _font_set(pymupdf, fontname, bold=bold)
    source_height = max(1.0, float(base_height or rect.height))
    if source_font_size and source_font_size > 0:
        base_size = max(4.0, min(30.0, float(source_font_size)))
    else:
        base_size = max(5.0, min(18.0, source_height * 0.72))
    display_text = text
    if indent and not display_text.startswith(("\u3000", " ")):
        display_text = "\u3000\u3000" + display_text
    sizes: list[float] = []
    size = float(base_size)
    while size >= 4.0:
        sizes.append(round(size, 1))
        size -= 0.5
    if not sizes:
        sizes = [4.0]

    max_width = max(1.0, float(rect.width) * 0.96 - 2.0)
    max_height = max(1.0, float(rect.height) - 1.0)
    for candidate_size in sizes:
        lines = _wrap_text(display_text, fonts, candidate_size, max_width)
        line_height = candidate_size * 1.18
        max_lines = max(1, int(max_height // line_height))
        if len(lines) <= max_lines:
            _draw_lines(
                page,
                rect,
                lines,
                fonts,
                candidate_size,
                bold=bold,
                justify=justify,
                max_width=max_width,
            )
            return candidate_size

    # Keep the beginning of an extremely long block and mark the truncation.
    chosen_size = 4.0
    lines = _wrap_text(display_text, fonts, chosen_size, max_width)
    line_height = chosen_size * 1.18
    max_lines = max(1, int(max_height // line_height))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].rstrip()
        while last and _rich_length(last + "…", fonts, chosen_size) > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    _draw_lines(
        page,
        rect,
        lines,
        fonts,
        chosen_size,
        bold=bold,
        justify=justify,
        max_width=max_width,
    )
    return chosen_size


def rebuild_pdf(
    source_path: str | Path,
    pages: list[dict[str, Any]],
    mono_path: str | Path,
    dual_path: str | Path,
    language: str,
) -> dict[str, Any]:
    """Create translated-only (mono) and interleaved (dual) PDFs."""

    pymupdf = _pymupdf()
    source_path = Path(source_path)
    mono_path = Path(mono_path).resolve()
    dual_path = Path(dual_path).resolve()
    mono_path.parent.mkdir(parents=True, exist_ok=True)
    dual_path.parent.mkdir(parents=True, exist_ok=True)

    fontname = font_for_language(language)
    source = pymupdf.open(str(source_path))
    translated = pymupdf.open()
    dual = None
    try:
        page_count = int(source.page_count)
        for page_index in range(page_count):
            src_page = source[page_index]
            # Keep the original page as the canvas so images, table rules,
            # figures and vector artwork stay in their exact positions.
            translated.insert_pdf(
                source,
                from_page=page_index,
                to_page=page_index,
            )
            out_page = translated[-1]

            page_payload = next(
                (p for p in pages if p.get("page") == page_index + 1),
                None,
            )
            source_blocks = (page_payload or {}).get("blocks", [])
            span_styles = extract_span_styles(src_page)
            bboxes: list[list[float] | None] = []
            for block in source_blocks:
                bbox = block.get("bbox")
                bboxes.append(
                    [float(v) for v in bbox]
                    if bbox and len(bbox) == 4
                    else None
                )
            layout_bboxes = _layout_bboxes(src_page.rect, bboxes)
            block_styles = []
            for block, bbox in zip(source_blocks, bboxes):
                if block.get("font_size") is not None:
                    block_styles.append(
                        {
                            "font_size": block.get("font_size"),
                            "bold": bool(block.get("bold", False)),
                        }
                    )
                else:
                    block_styles.append(
                        style_for_bbox(span_styles, bbox) if bbox else {}
                    )

            body_sizes = []
            for block, style in zip(source_blocks, block_styles):
                original = str(
                    block.get("original_text") or block.get("text") or ""
                ).strip()
                size = style.get("font_size")
                if size and not style.get("bold") and len(original) >= 20:
                    body_sizes.append(float(size))
            body_size = statistics.median(body_sizes) if body_sizes else 10.0

            # One page-level redaction is much faster than annotating hundreds
            # of text blocks. images=0 / graphics=0 preserve figures/tables.
            if any(bbox is not None for bbox in bboxes):
                out_page.add_redact_annot(
                    out_page.rect,
                    fill=None,
                    cross_out=False,
                )
                out_page.apply_redactions(
                    images=0,
                    graphics=0,
                    text=0,
                )

            for block_index, block in enumerate(source_blocks):
                text = (block.get("translated_text") or "").strip()
                bbox = bboxes[block_index]
                layout_bbox = layout_bboxes[block_index]
                if not text or bbox is None or layout_bbox is None:
                    continue
                style = block_styles[block_index]
                font_size = style.get("font_size")
                bold = block.get("bold")
                if bold is None:
                    bold = style.get("bold", False)
                original_text = str(
                    block.get("original_text") or block.get("text") or ""
                ).strip()
                ends_sentence = original_text.endswith(
                    (".", "。", "?", "？", "!", "！", ";", "；")
                )
                is_heading = bool(
                    original_text
                    and len(original_text) <= 60
                    and not ends_sentence
                    and (
                        bold
                        or (
                            font_size
                            and float(font_size) >= body_size * 1.08
                        )
                    )
                )
                if is_heading and (
                    not font_size or float(font_size) < body_size * 1.2
                ):
                    font_size = max(
                        float(font_size or body_size),
                        body_size * 1.2,
                        11.5,
                    )
                is_list = bool(
                    re.match(r"^\s*(?:[-•*]|\d+[.)])\s+", original_text)
                )
                is_paragraph = (
                    not is_heading
                    and not is_list
                    and len(original_text) >= 20
                )
                safe_rect = _safe_rect(
                    src_page.rect,
                    layout_bbox,
                    layout_bboxes,
                    block_index,
                )
                source_line_count = max(1, original_text.count("\n") + 1)
                source_line_height = (bbox[3] - bbox[1]) / source_line_count
                _draw_fitted(
                    out_page,
                    safe_rect,
                    text,
                    fontname,
                    base_height=source_line_height,
                    source_font_size=font_size,
                    bold=bool(bold),
                    indent=is_paragraph,
                    justify=is_paragraph,
                )

        translated.save(str(mono_path), garbage=3, deflate=True)

        dual = pymupdf.open()
        for page_index in range(page_count):
            dual.insert_pdf(source, from_page=page_index, to_page=page_index)
            dual.insert_pdf(translated, from_page=page_index, to_page=page_index)
        dual.save(str(dual_path), garbage=3, deflate=True)

        return {
            "mono_path": str(mono_path),
            "dual_path": str(dual_path),
            "page_count": page_count,
        }
    finally:
        try:
            source.close()
        except Exception:
            pass
        try:
            translated.close()
        except Exception:
            pass
        if dual is not None:
            try:
                dual.close()
            except Exception:
                pass
