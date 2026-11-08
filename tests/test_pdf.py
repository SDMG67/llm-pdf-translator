import pymupdf

from pdf_rebuild import rebuild_pdf
from pdf_service import parse_pdf_bytes


def make_pdf_bytes(text: str = "Hello PDF") -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    data = document.tobytes()
    document.close()
    return data


def make_multiblock_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    for text, y in (
        ("First line of text", 72),
        ("Second line of text", 150),
        ("Third line of text", 228),
    ):
        page.insert_text((72, y), text, fontsize=14)
    data = document.tobytes()
    document.close()
    return data


def make_two_column_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    for text, x, y in (
        ("Left column first block", 50, 100),
        ("Left column second block", 50, 150),
        ("Right column first block", 330, 220),
        ("Right column second block", 330, 270),
    ):
        page.insert_text((x, y), text, fontsize=12)
    data = document.tobytes()
    document.close()
    return data


def make_styled_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 60), "Large Title", fontsize=22, fontname="hebo")
    page.insert_text(
        (72, 130),
        "This is a long bold paragraph that should use the normal body size.",
        fontsize=11,
        fontname="hebo",
    )
    page.insert_text(
        (72, 180),
        "This is a normal paragraph with the same body font size.",
        fontsize=11,
        fontname="helv",
    )
    data = document.tobytes()
    document.close()
    return data


def make_graphics_pdf() -> bytes:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 80), "Table caption", fontsize=12, fontname="helv")
    page.draw_rect(
        pymupdf.Rect(72, 120, 520, 330),
        color=(0.2, 0.2, 0.2),
        fill=(0.95, 0.95, 0.95),
        width=1,
    )
    page.draw_line((72, 180), (520, 180), color=(0.2, 0.2, 0.2), width=1)
    data = document.tobytes()
    document.close()
    return data


async def test_pdf_parse_returns_pages_and_blocks(client):
    response = await client.post(
        "/pdf/parse",
        files={
            "file": (
                "sample.pdf",
                make_pdf_bytes("Hello PDF parse"),
                "application/pdf",
            )
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["page_count"] == 1
    assert body["parsed_pages"] == 1
    assert body["block_count"] >= 1
    assert "Hello PDF parse" in body["text"]
    assert body["blocks"][0]["bbox"]
    assert body["blocks"][0]["page"] == 1


async def test_pdf_parse_rejects_non_pdf(client):
    response = await client.post(
        "/pdf/parse",
        files={"file": ("bad.pdf", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


async def test_pdf_translate_mock_and_download(client):
    response = await client.post(
        "/pdf/translate",
        files={
            "file": (
                "sample.pdf",
                make_pdf_bytes("Hello PDF translate"),
                "application/pdf",
            )
        },
        data={"translate_model": "mock", "language": "zh"},
    )
    assert response.status_code == 200
    task_id = response.json()["task_id"]

    result = await client.get(f"/pdf/result/{task_id}")
    assert result.status_code == 200
    payload = result.json()
    assert payload["status"] == "SUCCESS"
    data = payload["data"]
    assert data["job_type"] == "pdf"
    assert data["block_count"] >= 1
    assert data["translated_block_count"] == data["block_count"]

    download = await client.get(f"/pdf/download/{task_id}")
    assert download.status_code == 200
    translated = download.json()
    assert translated["blocks"][0]["translated_text"].startswith("[zh] ")
    assert translated["pages"][0]["blocks"][0]["translated_text"]


async def test_pdf_translate_rebuilds_mono_and_dual(client):
    response = await client.post(
        "/pdf/translate",
        files={
            "file": (
                "sample.pdf",
                make_pdf_bytes("Hello PDF rebuild"),
                "application/pdf",
            )
        },
        data={"translate_model": "mock", "language": "zh"},
    )
    assert response.status_code == 200
    task_id = response.json()["task_id"]

    result = await client.get(f"/pdf/result/{task_id}")
    assert result.status_code == 200
    data = result.json()["data"]
    assert data["mono_path"]
    assert data["dual_path"]

    mono = await client.get(f"/pdf/download/{task_id}?format=mono")
    assert mono.status_code == 200
    assert "application/pdf" in mono.headers.get("content-type", "")
    mono_doc = pymupdf.open(stream=mono.content, filetype="pdf")
    assert mono_doc.page_count == 1
    assert "[zh]" in mono_doc[0].get_text()
    mono_doc.close()

    dual = await client.get(f"/pdf/download/{task_id}?format=dual")
    assert dual.status_code == 200
    dual_doc = pymupdf.open(stream=dual.content, filetype="pdf")
    assert dual_doc.page_count == 2
    dual_doc.close()


async def test_pdf_rebuild_keeps_blocks_separate(client):
    response = await client.post(
        "/pdf/translate",
        files={
            "file": (
                "multi.pdf",
                make_multiblock_pdf(),
                "application/pdf",
            )
        },
        data={"translate_model": "mock", "language": "zh"},
    )
    assert response.status_code == 200
    task_id = response.json()["task_id"]

    mono = await client.get(f"/pdf/download/{task_id}?format=mono")
    assert mono.status_code == 200
    document = pymupdf.open(stream=mono.content, filetype="pdf")
    page = document[0]
    flat_text = " ".join(page.get_text().split())
    for phrase in (
        "[zh] First line of text",
        "[zh] Second line of text",
        "[zh] Third line of text",
    ):
        assert phrase in flat_text

    blocks = [
        block
        for block in page.get_text("blocks", sort=True)
        if int(block[6]) == 0 and block[4].strip()
    ]
    assert len(blocks) >= 3
    for previous, current in zip(blocks, blocks[1:]):
        assert current[1] >= previous[3] - 1.0, (
            f"text blocks overlap: {previous} -> {current}"
        )
    document.close()


def test_rebuild_long_chinese_stays_within_source_blocks(tmp_path):
    source_bytes = make_multiblock_pdf()
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(source_bytes)
    parsed = parse_pdf_bytes(source_bytes, "source.pdf")
    translations = [
        "第一行中文内容比较长一些，需要换行显示并保持在原块区域",
        "第二行中文内容也比较长，需要换行但仍保持在第二块区域",
        "第三行中文内容",
    ]
    for block, translated in zip(parsed["blocks"], translations):
        block["translated_text"] = translated

    mono_path = tmp_path / "mono.pdf"
    dual_path = tmp_path / "dual.pdf"
    rebuild_pdf(
        source_path,
        parsed["pages"],
        mono_path,
        dual_path,
        "zh",
    )

    document = pymupdf.open(mono_path)
    page = document[0]
    source_tops = sorted(block["bbox"][1] for block in parsed["blocks"])
    line_boxes = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if line.get("spans"):
                line_boxes.append(line["bbox"])

    groups: list[list[list[float]]] = [[] for _ in source_tops]
    for bbox in line_boxes:
        top = bbox[1]
        for index, source_top in enumerate(source_tops):
            next_top = (
                source_tops[index + 1]
                if index + 1 < len(source_tops)
                else float("inf")
            )
            if source_top - 1 <= top < next_top:
                groups[index].append(bbox)
                break

    assert all(groups), groups
    for index in range(len(groups) - 1):
        assert max(box[3] for box in groups[index]) <= source_tops[index + 1] + 1
    document.close()


def test_rebuild_two_column_text_stays_in_its_column(tmp_path):
    source_bytes = make_two_column_pdf()
    source_path = tmp_path / "columns.pdf"
    source_path.write_bytes(source_bytes)
    parsed = parse_pdf_bytes(source_bytes, "columns.pdf")
    for block in parsed["blocks"]:
        block["translated_text"] = (
            "这是一段较长的中文译文，需要自动换行并严格限制在所属栏内，"
            "不能越过两栏之间的空白区域。"
        )

    mono_path = tmp_path / "columns_mono.pdf"
    dual_path = tmp_path / "columns_dual.pdf"
    rebuild_pdf(source_path, parsed["pages"], mono_path, dual_path, "zh")

    document = pymupdf.open(mono_path)
    page = document[0]
    center = (page.rect.x0 + page.rect.x1) / 2
    right_limit = page.rect.x1 - 18
    line_boxes = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if line.get("spans"):
                line_boxes.append(line["bbox"])

    left_lines = [box for box in line_boxes if box[0] < center]
    right_lines = [box for box in line_boxes if box[0] >= center]
    assert left_lines
    assert right_lines
    assert max(box[2] for box in left_lines) <= center + 1
    assert max(box[2] for box in right_lines) <= right_limit + 1
    document.close()


def test_rebuild_preserves_title_size_and_bold_metadata(tmp_path):
    source_bytes = make_styled_pdf()
    source_path = tmp_path / "styled.pdf"
    source_path.write_bytes(source_bytes)
    parsed = parse_pdf_bytes(source_bytes, "styled.pdf")
    by_text = {block["text"]: block for block in parsed["blocks"]}
    assert by_text["Large Title"]["font_size"] >= 20
    assert by_text["Large Title"]["bold"] is True
    assert by_text[
        "This is a long bold paragraph that should use the normal body size."
    ]["bold"] is True
    assert by_text[
        "This is a normal paragraph with the same body font size."
    ]["bold"] is False

    for block, translated in zip(
        parsed["blocks"],
        ["大标题", "粗体正文", "普通正文"],
    ):
        block["translated_text"] = translated
    mono_path = tmp_path / "styled_mono.pdf"
    dual_path = tmp_path / "styled_dual.pdf"
    rebuild_pdf(source_path, parsed["pages"], mono_path, dual_path, "zh")

    document = pymupdf.open(mono_path)
    spans = [
        span
        for block in document[0].get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if span.get("text", "").strip()
    ]
    def span_size(token: str) -> float:
        return next(
            span["size"]
            for span in spans
            if token in span.get("text", "")
        )

    assert span_size("大标题") > span_size("普通正文")
    assert abs(span_size("粗体正文") - span_size("普通正文")) <= 1.0
    document.close()


def test_rebuild_preserves_source_vector_graphics(tmp_path):
    source_bytes = make_graphics_pdf()
    source_path = tmp_path / "graphics.pdf"
    source_path.write_bytes(source_bytes)
    parsed = parse_pdf_bytes(source_bytes, "graphics.pdf")
    for block in parsed["blocks"]:
        block["translated_text"] = "表格标题"

    mono_path = tmp_path / "graphics_mono.pdf"
    dual_path = tmp_path / "graphics_dual.pdf"
    rebuild_pdf(source_path, parsed["pages"], mono_path, dual_path, "zh")

    document = pymupdf.open(mono_path)
    page = document[0]
    assert "表格标题" in page.get_text()
    assert len(page.get_drawings()) >= 2
    document.close()
