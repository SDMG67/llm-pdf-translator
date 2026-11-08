# LLM Translator（大模型翻译服务）

一个异步的大模型翻译服务：**FastAPI 后端 + Celery 任务队列 + 可插拔翻译引擎（策略模式）+ Gradio 前端**，并内置翻译缓存与按翻译引擎的 QPS 限流。

## 架构

```text
浏览器（gui.py, Gradio）
   |  POST /translator/create   GET /translator/get/{id}   DELETE /translator/{id}
   v
FastAPI（main.py + translator.py）
   |  1. 校验翻译引擎
   |  2. 由 cache_params() 生成缓存键
   |  3. 命中缓存 -> 直接返回
   v
Celery broker（开发用 SQLite / 生产用 Redis）
   v
Celery worker（celery_app.py）
   |  engine.translate(): 限流(QPS) -> do_translate() -> LLM
   |  成功后：写入缓存（同步会话）
   v
翻译引擎注册表（translators/）
   |- mock / echo / upper     （免费，用于测试整条链路）
   |- google / bing           （免费网页接口，无需 API Key）
   |- deepseek  （DEEPSEEK_* 变量，重试 + 限流 + token 用量统计）
   +- openai    （OPENAI_* 变量，官方端点或兼容网关）
```

`google` 与 `bing` 引擎使用公开的网页翻译接口，无需 API Key。但它们属于非官方接口，可能被限流或不可用：

- Bing 优先请求 `cn.bing.com`，失败时回退到 `www.bing.com`。
- Google 在部分网络环境下可能被屏蔽；无法直连时，请设置 `HTTPS_PROXY`/`HTTP_PROXY`，或覆盖 `GOOGLE_TRANSLATE_ENDPOINT`。

## 项目结构

| 路径 | 作用 |
|------|------|
| `main.py` | FastAPI 应用：CORS、/health、/cache CRUD、路由挂载 |
| `translator.py` | `/translator/*` 路由（创建 / 查询 / 取消） |
| `celery_app.py` | Celery 任务定义 |
| `backend.py` | Celery 应用配置（broker、backend） |
| `database.py` | 异步引擎（API 侧）+ 同步引擎（worker 侧） |
| `cache.py` | 缓存键构建 + 异步/同步缓存访问 |
| `pdf_service.py` | PDF 校验、页面/文本块提取、限制 |
| `pdf_router.py` | `/pdf/*` 上传、翻译、结果与下载路由 |
| `pdf_rebuild.py` | 保留排版的 mono/dual PDF 重建 |
| `schema.py` | Pydantic 请求模型 + SQLAlchemy 模型 |
| `rate_limiter.py` | 共享的按引擎 QPS 限流器 |
| `translators/` | BaseTranslator + 各引擎实现 + 注册表 |
| `gui.py` | Gradio 前端 |
| `requirements-core.txt` | API/worker 运行时依赖 |
| `requirements-gui.txt` | 仅 Gradio 依赖 |
| `Dockerfile` | 核心镜像（API + worker） |
| `Dockerfile.gui` | 轻量 GUI 镜像 |
| `tests/` | pytest 测试套件（以 Celery eager 模式运行，无需 broker） |

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/translator/create` | 创建翻译任务（或命中缓存直接返回） |
| GET | `/translator/get/{task_id}` | 轮询任务状态 / 结果 |
| DELETE | `/translator/{task_id}` | 取消运行中的任务 |
| POST | `/pdf/parse` | 上传 PDF 并提取页面/文本块 |
| POST | `/pdf/translate` | 上传 PDF 并异步翻译其文本块 |
| GET | `/pdf/result/{task_id}` | 轮询 PDF 翻译任务 |
| GET | `/pdf/download/{task_id}` | 下载 mono/dual PDF 或 JSON（`?format=mono\|dual\|json`） |
| POST | `/cache` | 新增记录（历史遗留） |
| GET | `/cache` | 列出记录（历史遗留） |
| DELETE | `/cache/{id}` | 删除记录（历史遗留） |

## PDF 上传与解析

`/pdf/parse` 接收 `multipart/form-data`，其中 `file` 字段携带文件。返回内容：

- 总页数与实际解析页数
- 每页的宽/高及提取出的文本
- 文本块：包含页码、bbox、块 id 与文本
- 截断标志与解析器元数据

`/pdf/translate` 接收同样的文件，外加 `translate_model`、`language`、可选的 `model` 和可选的 `ignore_cache`。它在 Celery worker 中解析 PDF 并逐块翻译。翻译完成后，会在原始文本块矩形内填入译文来重建 PDF：

- `GET /pdf/download/{task_id}?format=mono` —— 仅译文版 PDF
- `GET /pdf/download/{task_id}?format=dual` —— 原文 + 译文对照页
- `GET /pdf/download/{task_id}?format=json` —— 逐块翻译的 JSON

mono PDF 对 `zh`/`ja`/`ko` 使用内置 CJK 字体，而拉丁字母、数字和 URL 则用 Helvetica 绘制，以保持字形与字距正常。原始 span 的字号与加粗标记会被保留：拉丁文加粗使用 Helvetica Bold，中文加粗则用轻微的 CJK 描边在不改变字号的前提下实现。双栏页面会被识别，每个文本块被裁剪到各自栏内，跨栏的标题/图注则保持整页宽度。每个译文块保持原顶部位置，只能向下扩展到下一个块的上边沿；PyMuPDF 会在该安全区域内自动换行/缩放文本，若仍放不下则用省略号截断。正文段落使用两字符首行缩进，非末行在有限间距内两端对齐，以避免英文缩写造成过大间距。无文本层的扫描版 PDF 仍需 OCR。

Gradio 前端会先通过 GUI 服务器拉取生成的文件，再提供给浏览器下载。这样在 Docker 环境中也能正常下载——在那里 `API_URL=http://api:8000` 是仅内部可达的主机名。

对于大文档，免费的 `bing`/`google` 引擎会用有界线程池批量翻译小段文本，而不是为每个文本块单独发一次请求。翻译失败的块会保留原文，并以 `failed_blocks` 上报，因此单次限流响应不会导致整份文档作废。Gradio 的 PDF 轮询最长等待 `PDF_POLL_SECONDS`（默认 3600）秒，并在处理大文档时显示批量进度。

对于 mono PDF，以原始页面作为画布。只对源文本矩形做脱字（redact）；随后在叠加译文之前，图片、图形、表格线和矢量图形都保留在原位。

## 快速开始（本地开发）

1. 创建虚拟环境并安装依赖：

   ```bash
   python -m venv .venv
   # Windows: .\.venv\Scripts\Activate.ps1
   # Unix:    source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. 配置环境变量：

   ```bash
   cp env.example .env   # 然后按需填写 DEEPSEEK_API_KEY / OPENAI_API_KEY
   ```

3. 在三个终端分别启动三个进程：

   ```bash
   uvicorn main:app --reload
   celery -A backend worker -l info --pool solo
   python gui.py            # 打开 http://127.0.0.1:7860
   ```

## Docker 部署（类生产环境）

```bash
docker compose up --build -d
# API: http://127.0.0.1:8000
# GUI: http://127.0.0.1:7860

# 仅后端，首次构建更快（跳过 Gradio）：
docker compose up --build -d redis api worker
docker compose down
```

默认启动四个服务：`redis`（broker）、`api`（FastAPI）、`worker`（Celery）和 `gui`（Gradio）。SQLite/PDF 数据卷挂载在 `./data`。

### Docker 构建速度

完整栈使用两个镜像：

- `llm-translator-core:local` 用于 API + worker
- `llm-translator-gui:local` 仅用于 Gradio

默认的 `docker compose up --build -d` 会构建 core 与 GUI 两个镜像。worker 复用 core 镜像，无需再装一遍依赖。两个 Dockerfile 都使用了 BuildKit 的 `uv` 缓存挂载、跳过了多余的 pip 自升级，并默认使用 PyPI。若只需后端，首次构建用 `docker compose up --build -d redis api worker`，此时不会构建 Gradio。

使用其他镜像源：

```bash
  PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build
```

## 测试

```bash
pip install -r requirements-dev.txt
pytest -v
```

测试套件使用 Celery eager 模式与一个临时 SQLite 数据库，因此无需 broker 或网络访问。

## 新增翻译引擎

1. 新建 `translators/myengine.py`，继承 `BaseTranslator`，设置 `name`、实现 `do_translate`，如有影响翻译质量的可调参数则覆写 `cache_params`。
2. 在 `translators/registry.py` 中注册。
3. 把引擎名加入 `gui.py` 的 `ENGINES` 列表。
