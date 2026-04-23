import asyncio
import html as html_module
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import jinja2
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse

from bandcamp import download_url

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", "/music"))

executor = ThreadPoolExecutor(max_workers=int(os.environ.get("MAX_WORKERS", "3")))
jobs: dict[str, dict] = {}
_subscribers: list[asyncio.Queue] = []
_loop: asyncio.AbstractEventLoop | None = None

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader("templates"),
    autoescape=True,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(lifespan=lifespan)


# --- HTML fragment helpers ---

def _render_job(job: dict) -> str:
    return _env.get_template("_job.html").render(job=job)


def _oob_badge(job_id: str, status: str) -> str:
    return (
        f'<span id="badge-{job_id}" hx-swap-oob="outerHTML" class="job-badge badge-{status}">{status}</span>'
        f'<span id="dot-{job_id}" hx-swap-oob="outerHTML" class="job-dot dot-{status}"></span>'
    )


def _oob_log(job_id: str, msg: str) -> str:
    safe = html_module.escape(msg)
    return f'<div id="logs-{job_id}" hx-swap-oob="beforeend"><span class="log-line">{safe}</span>\n</div>'


def _oob_meta(job_id: str, job: dict) -> str:
    meta = job.get("meta") or {}
    error = job.get("error") or ""
    if meta.get("artist"):
        content = html_module.escape(meta["artist"])
        if meta.get("album"):
            content += f' — {html_module.escape(meta["album"])}'
    elif error:
        content = html_module.escape(error[:60])
    else:
        content = ""
    return f'<span id="meta-{job_id}" hx-swap-oob="outerHTML" class="job-meta">{content}</span>'


# --- SSE broadcast ---

def _broadcast(data: str, event: str | None = None) -> None:
    if _loop is None:
        return
    msg = f"event: {event}\ndata: {data}\n\n" if event else f"data: {data}\n\n"
    for q in list(_subscribers):
        asyncio.run_coroutine_threadsafe(q.put(msg), _loop)


# --- Download runner (thread) ---

def _run_download(job_id: str, url: str) -> None:
    jobs[job_id]["status"] = "downloading"
    _broadcast(_oob_badge(job_id, "downloading"))

    def progress(msg: str) -> None:
        jobs[job_id]["logs"].append(msg)
        _broadcast(_oob_log(job_id, msg))

    try:
        meta = download_url(url, MUSIC_DIR, progress)
        jobs[job_id]["status"] = "done"
        jobs[job_id]["meta"] = {k: meta.get(k) for k in ("artist", "album", "year")}
        _broadcast(_oob_badge(job_id, "done") + _oob_meta(job_id, jobs[job_id]))
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        _broadcast(_oob_badge(job_id, "error") + _oob_meta(job_id, jobs[job_id]))


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_env.get_template("index.html").render(jobs=list(jobs.values())))


@app.post("/download", response_class=HTMLResponse)
async def start_downloads(urls: str = Form(...)):
    loop = asyncio.get_running_loop()
    for url in (u.strip() for u in urls.splitlines() if u.strip()):
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"id": job_id, "url": url, "status": "queued", "logs": []}
        _broadcast(_render_job(jobs[job_id]), event="new-job")
        loop.run_in_executor(executor, _run_download, job_id, url)
    return ""  # textarea cleared via hx-on:htmx:after-request on the form


@app.delete("/jobs", response_class=HTMLResponse)
async def clear_done_jobs():
    done = [jid for jid, j in list(jobs.items()) if j["status"] in ("done", "error")]
    for jid in done:
        del jobs[jid]
    return "".join(
        f'<div id="job-{jid}" hx-swap-oob="delete"></div>' for jid in done
    )


@app.get("/events")
async def events() -> StreamingResponse:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.append(q)

    async def generator() -> AsyncGenerator[str, None]:
        for job in jobs.values():
            yield f"event: new-job\ndata: {_render_job(job)}\n\n"
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30)
                    yield msg
                except asyncio.TimeoutError:
                    yield "data: \n\n"  # keepalive
        except asyncio.CancelledError:
            pass
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
