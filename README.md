# LLM Translator

An async LLM translation service: FastAPI backend + Celery task queue + pluggable
translation engines (strategy pattern) + Gradio frontend, with translation caching
and per-engine QPS rate limiting.

## Architecture

```
Browser (gui.py, Gradio)
   |  POST /translator/create   GET /translator/get/{id}   DELETE /translator/{id}
   v
FastAPI (main.py + translator.py)
   |  1. validate engine
   |  2. build cache key from cache_params()
   |  3. cache hit -> return immediately
   v
Celery broker (SQLite for dev / Redis for prod)
   v
Celery worker (celery_app.py)
   |  engine.translate(): throttle(QPS) -> do_translate() -> LLM
   |  on success: write cache (sync session)
   v
Engine registry (translators/)
   |- mock / echo / upper     (free, for testing the wiring)
   |- google / bing           (free web endpoints, no API key)
   |- deepseek  (DEEPSEEK_* vars, retry + rate limit + token usage)
   +- openai    (OPENAI_* vars,   official endpoint or gateway)
```

The `google` and `bing` engines use public web translation endpoints and need
no API key. They are unofficial and may be rate-limited or unavailable:

- Bing checks `cn.bing.com` first and falls back to `www.bing.com`.
- Google can be blocked by some networks; set `HTTPS_PROXY`/`HTTP_PROXY`, or
  override `GOOGLE_TRANSLATE_ENDPOINT`, when direct access is unavailable.

## Project layout

| Path | Purpose |
|------|---------|
| `main.py` | FastAPI app: CORS, /health, /cache CRUD, router include |
| `translator.py` | `/translator/*` routes (create / get / cancel) |
| `celery_app.py` | Celery task definitions |
| `backend.py` | Celery app config (broker, backend) |
| `database.py` | Async engine (API) + sync engine (worker) |
| `cache.py` | Cache key builder + async/sync cache access |
| `pdf_service.py` | PDF validation, page/block extraction, limits |
| `pdf_router.py` | `/pdf/*` upload, translate, result and download routes |
| `pdf_rebuild.py` | Layout-preserving mono/dual PDF reconstruction |
| `schema.py` | Pydantic request model + SQLAlchemy models |
| `rate_limiter.py` | Shared per-engine QPS limiter |
| `translators/` | BaseTranslator + engine implementations + registry |
| `gui.py` | Gradio frontend |
| `requirements-core.txt` | API/worker runtime dependencies |
| `requirements-gui.txt` | Gradio-only dependencies |
| `Dockerfile` | Core image (API + worker) |
| `Dockerfile.gui` | Lightweight GUI image |
| `tests/` | pytest suite (runs Celery eagerly, no broker needed) |

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/translator/create` | Create a translation task (or return cache hit) |
| GET | `/translator/get/{task_id}` | Poll task status / result |
| DELETE | `/translator/{task_id}` | Cancel a running task |
| POST | `/pdf/parse` | Upload a PDF and extract pages/text blocks |
| POST | `/pdf/translate` | Upload a PDF and translate its text blocks asynchronously |
| GET | `/pdf/result/{task_id}` | Poll a PDF translation task |
| GET | `/pdf/download/{task_id}` | Download mono/dual PDF or JSON (`?format=mono\|dual\|json`) |
| POST | `/cache` | Add a record (legacy) |
| GET | `/cache` | List records (legacy) |
| DELETE | `/cache/{id}` | Delete a record (legacy) |

Interactive docs: `http://127.0.0.1:8000/docs`

## PDF upload and parsing

`/pdf/parse` accepts `multipart/form-data` with a `file` field. It returns:

- page count and parsed page count
- per-page width/height and extracted text
- text blocks with page, bbox, block id and text
- truncation flags and parser metadata

`/pdf/translate` accepts the same file plus `translate_model`, `language`,
optional `model`, and optional `ignore_cache`. It parses the PDF in the Celery
worker and translates each block. After translation it rebuilds the PDF with
the translated text inside the original block rectangles:

- `GET /pdf/download/{task_id}?format=mono` — translated-only PDF
- `GET /pdf/download/{task_id}?format=dual` — original + translated pages
- `GET /pdf/download/{task_id}?format=json` — per-block translation JSON

The mono PDF uses a built-in CJK font for `zh`/`ja`/`ko`, while Latin letters,
digits and URLs are drawn with Helvetica so their glyph shapes and letter
spacing stay normal. Original span sizes and bold flags are preserved:
Helvetica Bold is used for Latin bold text, and a light CJK stroke reproduces
Chinese bold without changing font size. Two-column pages are detected and each block is clipped
to its own column, with cross-column titles/captions kept at page width. Each
translated block keeps its source top and may only grow down to the next
block's top edge; PyMuPDF wraps/shrinks text inside that safe region, then
truncates with an ellipsis if it still cannot fit. Body paragraphs use a
two-character first-line indent and non-final lines are justified with a
bounded gap so English abbreviations do not create extreme spacing. Scanned
PDFs without a text layer still need OCR.

The Gradio frontend fetches generated files through the GUI server before
offering them to the browser. This keeps downloads working in Docker, where
`API_URL=http://api:8000` is an internal-only hostname.

For large documents, the free `bing`/`google` engines translate small batches
with a bounded thread pool instead of issuing one request per text block.
Failed blocks keep their source text and are reported as `failed_blocks`, so a
single rate-limit response does not discard the whole document. The Gradio
PDF poller waits up to `PDF_POLL_SECONDS` (default 3600) and shows batch
progress while a large document is being processed.

For mono PDFs the original page is used as the canvas. Only the source text
rectangles are redacted; embedded images, figures, table rules and vector
artwork are preserved in their original positions before translated text is
overlaid.

## Quick start (local dev)

1. Create a virtualenv and install deps:

   ```bash
   python -m venv .venv
   # Windows: .\.venv\Scripts\Activate.ps1
   # Unix:    source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Configure env:

   ```bash
   cp env.example .env   # then fill in DEEPSEEK_API_KEY / OPENAI_API_KEY if needed
   ```

3. Run the three processes in separate terminals:

   ```bash
   uvicorn main:app --reload
   celery -A backend worker -l info --pool solo
   python gui.py            # open http://127.0.0.1:7860
   ```

## Docker (production-like)

```bash
docker compose up --build -d
# API: http://127.0.0.1:8000
# GUI: http://127.0.0.1:7860

# Backend-only, faster first build (skip Gradio):
docker compose up --build -d redis api worker
docker compose down
```

The default starts four services: `redis` (broker), `api` (FastAPI),
`worker` (Celery) and `gui` (Gradio). The SQLite/PDF data volume is mounted
at `./data`.

### Docker build speed

The full stack uses two images:

- `llm-translator-core:local` for API + worker
- `llm-translator-gui:local` for Gradio only

The default `docker compose up --build -d` builds the core and GUI images.
The worker reuses the core image instead of running another dependency
install. Both Dockerfiles use a BuildKit `uv` cache mount, skip the unnecessary
pip self-upgrade, and default to PyPI. For a backend-only first build, use
`docker compose up --build -d redis api worker`; Gradio is then not built.

To use another index:

```bash
  PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -v
```

The suite uses Celery eager mode and a throwaway SQLite DB, so no broker or
network access is required.

## Adding a new engine

1. Create `translators/myengine.py`, subclass `BaseTranslator`, set `name`,
implement `do_translate`, and override `cache_params` if quality-affecting
parameter matter.
2. Register it in `translators/registry.py`.
3. Add its name to `ENGINES` in `gui.py`.
