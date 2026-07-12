"""FastAPI app: upload endpoint, job list/detail, static package serving, and
the single-page UI. Deliberately free of torch / audio_separator imports (see
web/jobs.py's docstring) so `import stemlab.web.app` stays fast -- the actual
separation always happens in a subprocess of the existing `stemlab` CLI.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from stemlab.web.jobs import Job, JobStore, Runner, safe_filename

# Audio formats plus the mp4/mov video containers, case-insensitive: the
# pipeline normalizes through ffmpeg, which extracts the audio track from a
# video file just as happily (verified against a real .mp4).
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov"}
# 500MB: video uploads are legitimately much larger than audio-only files
# (a few minutes of 1080p can pass 200MB); local-only server, so be generous.
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 1 << 20

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _elapsed_seconds(job: Job) -> Optional[float]:
    if not job.started_at:
        return None
    start = datetime.fromisoformat(job.started_at)
    end = datetime.fromisoformat(job.finished_at) if job.finished_at else datetime.now(timezone.utc)
    return max(0.0, (end - start).total_seconds())


def _serialize_job(job: Job) -> dict:
    return {
        "id": job.id,
        "title": job.title,
        "target": job.target,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "elapsed_seconds": _elapsed_seconds(job),
        "package_url": f"/packages/{job.package}" if job.package else None,
        "error": job.error,
    }


def create_app(out_dir: Path, runner: Optional[Runner] = None) -> FastAPI:
    """Build a configured FastAPI app. `out_dir` is where practice packages,
    uploads, job records and logs all live (the same directory the `stemlab`
    CLI's `-o` points at). `runner` lets tests inject a fake subprocess
    launcher instead of actually running the CLI (see web/jobs.py)."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    store = JobStore(out_dir, runner=runner) if runner is not None else JobStore(out_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Graceful stop (Ctrl-C / SIGTERM): take the running separation
        # subprocess down with us instead of orphaning it -- see
        # JobStore.shutdown / the sidecar reaping in jobs.py.
        store.shutdown()

    app = FastAPI(title="StemLab Web", lifespan=lifespan)
    app.state.job_store = store
    app.state.out_dir = out_dir

    # The server only ever binds 127.0.0.1, but a browser can still be lured
    # into sending requests here from a hostile page (CSRF) or via DNS
    # rebinding, where an attacker domain resolves to 127.0.0.1 and becomes
    # same-origin with us. Host-header pinning closes the rebinding read
    # path. "testserver" is Starlette's TestClient default host; it has no
    # public DNS resolution, so allowing it costs nothing outside tests.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "::1", "testserver"],
    )

    # /packages must expose only the practice packages. out_dir also holds
    # web/ (original uploads, job records, logs) and .cache/ (intermediate
    # stems) -- serving those would hand the uploaded source audio to anyone
    # who can make the browser fetch from us. Compare on normalized path
    # segments, not string prefixes, so "//web/..." can't slip through.
    _BLOCKED_TOPDIRS = {"web", ".cache"}

    @app.middleware("http")
    async def _block_private_package_paths(request: Request, call_next):
        segments = [s for s in request.url.path.split("/") if s and s != "."]
        if len(segments) >= 2 and segments[0] == "packages" and (
            segments[1] in _BLOCKED_TOPDIRS or ".." in segments
        ):
            return PlainTextResponse("Not Found", status_code=404)
        return await call_next(request)

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html.j2", {})

    @app.post("/api/jobs")
    async def create_job(
        file: UploadFile = File(...), title: Optional[str] = Form(None)
    ) -> JSONResponse:
        filename = file.filename or ""
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported file type: {ext or '(none)'}; allowed: "
                + ", ".join(sorted(ALLOWED_EXTENSIONS)),
            )

        store.uploads_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=store.uploads_dir, suffix=ext)
        tmp_path = Path(tmp_name)
        digest_hash = hashlib.sha1()
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = await file.read(UPLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"file too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)}MB)",
                        )
                    digest_hash.update(chunk)
                    out.write(chunk)
        except HTTPException:
            tmp_path.unlink(missing_ok=True)
            raise
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        digest = digest_hash.hexdigest()
        dest = store.uploads_dir / f"{digest}{ext}"
        if dest.exists():
            tmp_path.unlink(missing_ok=True)  # already have this exact content on disk
        else:
            os.replace(tmp_path, dest)  # atomic rename on the same filesystem

        requested_title = (title or "").strip() or Path(filename).stem or "untitled"
        job, created = store.create_job(dest, digest, requested_title, target="guitar")

        return JSONResponse(
            {"job_id": job.id, "dedup": not created},
            status_code=202 if created else 200,
        )

    @app.get("/api/jobs")
    def list_jobs() -> list[dict]:
        return [_serialize_job(j) for j in store.list_jobs()]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _serialize_job(job)

    # out_dir itself (not a subfolder) so completed packages, wherever their
    # safe-titled subfolder lands, are reachable at /packages/<safe>/<file>.
    # html=False (Starlette default) means no directory-listing / index.html
    # auto-serving -- only exact file paths resolve.
    app.mount("/packages", StaticFiles(directory=str(out_dir), html=False), name="packages")

    return app
