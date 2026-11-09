import os
import tempfile
import threading
import time
from pathlib import Path

import gradio as gr
import requests

# 后端地址；Docker 内由 API_URL=http://api:8000 指定。
API = os.environ.get("API_URL", "http://127.0.0.1:8000")
PDF_POLL_SECONDS = int(os.environ.get("PDF_POLL_SECONDS", "3600"))

ENGINES = ["mock", "google", "bing", "echo", "upper", "deepseek", "openai"]
ENGINE_LABELS = {
    "mock": "模拟测试",
    "google": "Google 免费",
    "bing": "Bing 免费",
    "echo": "回声测试",
    "upper": "大写测试",
    "deepseek": "DeepSeek",
    "openai": "OpenAI",
}
ENGINE_CHOICES = [(ENGINE_LABELS[name], name) for name in ENGINES]

LANGS = ["zh", "en", "ja", "ko", "fr", "de", "es", "ru"]
LANG_LABELS = {
    "zh": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "ru": "俄语",
}
LANG_CHOICES = [(LANG_LABELS[code], code) for code in LANGS]

# 每个任务的取消信号。
_cancel_events: dict[str, threading.Event] = {}


def _render_usage(usage: dict | None) -> str:
    """把 token 用量显示为中文摘要。"""
    if not usage:
        return ""
    labels = {
        "total_tokens": "总 Token",
        "input_tokens": "输入",
        "output_tokens": "输出",
        "prompt_tokens": "提示",
        "completion_tokens": "生成",
    }
    parts = [
        f"{labels[key]}={usage[key]}"
        for key in labels
        if key in usage
    ]
    return "，".join(parts)


def request_cancel(state: dict) -> dict:
    """请求取消当前任务。"""
    task_id = state.get("task_id") if state else None
    if task_id:
        event = _cancel_events.get(task_id)
        if event:
            event.set()
    return state


def do_translate(
    text: str,
    model: str,
    lang: str,
    state: dict,
    progress: gr.Progress = gr.Progress(),
):
    """提交文本翻译任务并轮询结果。"""
    if not text or not text.strip():
        raise gr.Error("请输入要翻译的文本。")

    try:
        response = requests.post(
            f"{API}/translator/create",
            json={
                "original_text": text,
                "translate_model": model,
                "language": lang,
            },
            timeout=30,
        )
        payload = response.json()
    except Exception as exc:
        raise gr.Error(f"无法连接后端 {API}：{exc}")

    if payload.get("status") == "SUCCESS":
        data = payload.get("data", {})
        return state, data.get("translated_text", ""), "结果来自缓存"

    task_id = payload.get("task_id")
    if not task_id:
        raise gr.Error(f"创建任务失败：{payload}")

    event = threading.Event()
    _cancel_events[task_id] = event
    state = {"task_id": task_id}

    for index in range(600):
        if event.is_set():
            try:
                requests.delete(f"{API}/translator/{task_id}", timeout=10)
            except Exception:
                pass
            _cancel_events.pop(task_id, None)
            raise gr.Error("任务已取消。")

        try:
            result = requests.get(
                f"{API}/translator/get/{task_id}", timeout=30
            ).json()
        except Exception as exc:
            raise gr.Error(f"查询任务失败：{exc}")

        status = result.get("status")
        if status == "SUCCESS":
            data = result.get("data", {})
            usage = _render_usage(data.get("usage"))
            _cancel_events.pop(task_id, None)
            return state, data.get("translated_text", ""), usage or "翻译完成"

        if status in ("FAILURE", "REVOKED"):
            _cancel_events.pop(task_id, None)
            status_text = "失败" if status == "FAILURE" else "已取消"
            raise gr.Error(
                f"任务结束：{status_text} {result.get('error', '')}"
            )

        progress(
            (index % 100) / 100.0,
            desc="正在翻译...",
        )
        time.sleep(1)

    _cancel_events.pop(task_id, None)
    raise gr.Error("等待翻译结果超时。")


def do_translate_pdf(
    path: str | None,
    model: str,
    lang: str,
    progress: gr.Progress = gr.Progress(),
) -> tuple[str, str, str]:
    """上传 PDF 并返回双语对照、仅译文两个 PDF 文件。"""
    if not path:
        raise gr.Error("请先上传 PDF 文件。")

    filename = os.path.basename(path)
    try:
        with open(path, "rb") as handle:
            response = requests.post(
                f"{API}/pdf/translate",
                files={"file": (filename, handle, "application/pdf")},
                data={"translate_model": model, "language": lang},
                timeout=120,
            )
    except Exception as exc:
        raise gr.Error(f"PDF 上传失败：{exc}")

    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise gr.Error(
            f"PDF 翻译失败（{response.status_code}）：{detail}"
        )

    task_id = response.json()["task_id"]
    for index in range(PDF_POLL_SECONDS):
        try:
            result = requests.get(
                f"{API}/pdf/result/{task_id}", timeout=30
            ).json()
        except Exception as exc:
            raise gr.Error(f"查询 PDF 任务失败：{exc}")

        status = result.get("status")
        if status == "SUCCESS":
            # API_URL 在 Docker 中是内部主机名，浏览器无法直接访问。
            # 由 GUI 服务器先下载，再交给 Gradio 提供浏览器下载。
            out_dir = (
                Path(tempfile.gettempdir())
                / "llm_translator_gui"
                / task_id
            )
            dual_path = _download_pdf_artifact(
                task_id,
                "dual",
                f"{task_id}_dual.pdf",
                out_dir,
            )
            mono_path = _download_pdf_artifact(
                task_id,
                "mono",
                f"{task_id}_mono.pdf",
                out_dir,
            )
            progress(1.0, desc="PDF 翻译完成")
            return (
                f"**翻译完成**，任务：`{task_id}`。"
                "请在下方下载双语对照版或仅译文版。",
                dual_path,
                mono_path,
            )

        if status == "FAILURE":
            raise gr.Error(
                f"PDF 翻译失败：{result.get('error', '')}"
            )
        if status == "REVOKED":
            raise gr.Error("PDF 翻译任务已取消。")

        info = result.get("info") or {}
        current = info.get("current")
        total = info.get("total")
        if info.get("phase") == "rebuilding":
            progress(0.99, desc="正在重建 PDF 版面...")
        elif total:
            fraction = min(max(float(current or 0) / float(total), 0.0), 0.99)
            progress(
                fraction,
                desc=f"正在翻译 PDF... {current}/{total}",
            )
        else:
            progress(
                (index % 100) / 100.0,
                desc="正在翻译 PDF...",
            )
        time.sleep(1)

    raise gr.Error(
        "等待 PDF 翻译结果超时；任务可能仍在后台运行，请稍后重试或查看 worker 日志。"
    )


def _download_pdf_artifact(
    task_id: str,
    file_format: str,
    filename: str,
    out_dir: Path,
) -> str:
    """由 GUI 服务器下载 API 文件并返回本地路径。"""
    try:
        response = requests.get(
            f"{API}/pdf/download/{task_id}",
            params={"format": file_format},
            timeout=180,
            stream=True,
        )
    except Exception as exc:
        raise gr.Error(f"下载 PDF 失败：{exc}")

    if response.status_code != 200:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise gr.Error(
            f"下载 PDF 失败（{response.status_code}）：{detail}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    local_path = out_dir / filename
    with local_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
    if local_path.stat().st_size == 0:
        raise gr.Error("下载到的 PDF 文件为空。")
    return str(local_path)


with gr.Blocks(title="大模型翻译工具") as demo:
    gr.Markdown("# 大模型翻译工具")
    gr.Markdown(
        f"后端地址：`{API}` ｜ 免费引擎：Google、Bing ｜ "
        "请先启动 API 和 Celery worker。"
    )

    state = gr.State({})

    gr.Markdown("## 文本翻译")
    with gr.Row():
        with gr.Column(scale=1):
            text_in = gr.Textbox(lines=10, label="原文")
            with gr.Row():
                model = gr.Dropdown(
                    choices=ENGINE_CHOICES,
                    value="mock",
                    label="翻译引擎",
                )
                lang = gr.Dropdown(
                    choices=LANG_CHOICES,
                    value="zh",
                    label="目标语言",
                )
            with gr.Row():
                btn = gr.Button("翻译", variant="primary")
                cancel_btn = gr.Button("取消", variant="secondary")
        with gr.Column(scale=1):
            text_out = gr.Textbox(
                lines=10,
                label="译文",
                interactive=False,
            )
            meta = gr.Markdown("")

    btn.click(
        do_translate,
        inputs=[text_in, model, lang, state],
        outputs=[state, text_out, meta],
    )
    cancel_btn.click(
        request_cancel,
        inputs=[state],
        outputs=[state],
    )

    gr.Markdown("---")
    gr.Markdown(
        "## PDF 翻译\n"
        "上传 PDF，选择翻译引擎和目标语言，然后点击“翻译 PDF”。"
        "完成后可直接下载双语对照版和仅译文版。"
        "大文档可能需要较长时间，请保持页面打开。"
    )

    pdf_file = gr.File(
        label="上传 PDF",
        file_types=[".pdf"],
        type="filepath",
    )
    with gr.Row():
        pdf_engine = gr.Dropdown(
            choices=ENGINE_CHOICES,
            value="bing",
            label="翻译引擎",
        )
        pdf_lang = gr.Dropdown(
            choices=LANG_CHOICES,
            value="zh",
            label="目标语言",
        )
        pdf_translate_btn = gr.Button(
            "翻译 PDF",
            variant="primary",
        )

    pdf_status = gr.Markdown("")
    with gr.Row():
        pdf_dual = gr.File(
            label="双语对照 PDF（原文 + 译文）",
            interactive=False,
        )
        pdf_mono = gr.File(
            label="仅译文 PDF",
            interactive=False,
        )

    pdf_translate_btn.click(
        do_translate_pdf,
        inputs=[pdf_file, pdf_engine, pdf_lang],
        outputs=[pdf_status, pdf_dual, pdf_mono],
    )


if __name__ == "__main__":
    demo.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("GUI_PORT", "7860")),
        share=False,
    )
