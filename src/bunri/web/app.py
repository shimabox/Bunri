"""FastAPI app: upload endpoint, job list/detail, static package serving, and
the single-page UI. Deliberately free of torch / audio_separator imports (see
web/jobs.py's docstring) so `import bunri.web.app` stays fast -- the actual
separation always happens in a subprocess of the existing `bunri` CLI.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bunri.registry import REGISTRY
from bunri.pocket.config import read_config
from bunri.pocket.http import PocketHTTPClient
from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.service import (
    PocketServiceError,
    inspect_packages,
    resolve_package,
    safe_error,
)
from bunri.web.jobs import (
    Job,
    JobStore,
    Runner,
    Song,
    SongDeleteConflict,
    SongNotFoundError,
    safe_filename,
)

# Audio formats plus the mp4/mov video containers, case-insensitive: the
# pipeline normalizes through ffmpeg, which extracts the audio track from a
# video file just as happily (verified against a real .mp4).
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".mov", ".webm"}
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


def _target_label(target: str) -> str:
    return REGISTRY[target].label_ja if target in REGISTRY else target


def _download_files(job: Job) -> list[dict]:
    if job.status != "done" or not job.package:
        return []

    package_name = job.package.split("/", 1)[0]
    title = safe_filename(job.title)
    target_label = _target_label(job.target)
    downloads = []
    for track, label, suffix in (
        ("target", f"{target_label}のみ", ""),
        ("backing", f"{target_label}なし", ".backing"),
    ):
        files = []
        for audio_format in ("mp3", "wav"):
            audio_name = f"{package_name}.{job.target}{suffix}.{audio_format}"
            files.append(
                {
                    "format": audio_format,
                    "url": f"/packages/{quote(f'{package_name}/{audio_name}')}",
                    "filename": f"{title}_{label}.{audio_format}",
                }
            )
        downloads.append({"track": track, "label": label, "files": files})
    return downloads


def _serialize_job(job: Job) -> dict:
    if job.kind != "separate":
        return {
            "id": job.id,
            "kind": job.kind,
            "song_id": job.pocket_song_id,
            "status": job.status,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "elapsed_seconds": _elapsed_seconds(job),
            "progress": job.progress,
            "result": job.result,
            "error": job.error,
        }
    return {
        "id": job.id,
        "title": job.title,
        "target": job.target,
        "status": job.status,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "elapsed_seconds": _elapsed_seconds(job),
        # quote()'s default safe="/" keeps the path structure readable while
        # percent-encoding anything that would otherwise break the URL --
        # notably "#" (would truncate the URL at a fragment) and spaces --
        # in job.package, which comes straight from job.title's sanitized
        # slug (see safe_filename) and the user-controlled title can still
        # contain those characters even though the slug itself never does.
        "package_url": f"/packages/{quote(job.package)}" if job.package else None,
        "downloads": _download_files(job),
        "error": job.error,
    }


def _serialize_song(song: Song, pocket_job: Job | None = None) -> dict:
    targets = []
    for job in song.targets:
        serialized = _serialize_job(job)
        serialized.pop("title")
        serialized["target_label"] = _target_label(job.target)
        targets.append(serialized)
    result = {
        "id": song.id,
        "title": song.title,
        "created_at": song.created_at,
        "targets": targets,
    }
    result["pocket_job"] = _serialize_job(pocket_job) if pocket_job is not None else None
    return result


def create_app(out_dir: Path, runner: Optional[Runner] = None) -> FastAPI:
    """Build a configured FastAPI app. `out_dir` is where practice packages,
    uploads, job records and logs all live (the same directory the `bunri`
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

    app = FastAPI(title="Bunri Web", lifespan=lifespan)
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

    # CSRF: this server has no auth/session cookie to steal, but a POST from a
    # hostile page (or driven by DNS rebinding onto 127.0.0.1) could still
    # queue jobs / burn disk & CPU on someone else's behalf. Strict same-
    # origin enforcement on unsafe methods closes that off: the Origin header
    # is one browsers attach to every cross-origin request and cannot be
    # overridden by page script, so requiring it to name *this* origin
    # exactly (scheme+host+port) is sufficient -- no CSRF token needed for a
    # single-user local tool. Missing Origin (curl, same-origin non-CORS
    # requests some older browsers omit it for, etc.) is allowed: that header
    # is opt-in information a hostile *page* cannot suppress, so its absence
    # isn't itself suspicious the way a wrong value would be.
    _UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
    _DEFAULT_PORT_BY_SCHEME = {"http": 80, "https": 443}

    def _effective_port(scheme: str, port: Optional[int]) -> Optional[int]:
        return port if port is not None else _DEFAULT_PORT_BY_SCHEME.get(scheme)

    @app.middleware("http")
    async def _enforce_same_origin(request: Request, call_next):
        if request.method in _UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                try:
                    parsed = urlsplit(origin)
                    same_origin = (
                        bool(parsed.scheme)
                        and bool(parsed.hostname)
                        # .port parses lazily and raises ValueError on a
                        # malformed port ("http://host:not-a-port"), as does
                        # urlsplit itself on e.g. a broken IPv6 literal. An
                        # Origin we can't even parse is certainly not this
                        # origin, so it belongs in the 403 branch below --
                        # letting the ValueError escape would turn a hostile
                        # (or merely broken) header into a 500.
                        and parsed.scheme == request.url.scheme
                        and parsed.hostname == request.url.hostname
                        and _effective_port(parsed.scheme, parsed.port)
                        == _effective_port(request.url.scheme, request.url.port)
                    )
                except ValueError:
                    same_origin = False
                if not same_origin:
                    return PlainTextResponse("Forbidden", status_code=403)
        return await call_next(request)

    # /packages must expose only the practice packages. out_dir also holds
    # web/ (original uploads, job records, logs) and .cache/ (intermediate
    # stems) -- serving those would hand the uploaded source audio to anyone
    # who can make the browser fetch from us. Compare on normalized path
    # segments, not string prefixes, so "//web/..." can't slip through.
    # casefold() so a differently-cased alias of a blocked directory (e.g. a
    # literal "WEB" folder -- distinct from "web" on a case-sensitive Linux
    # filesystem, even though the two collide on macOS's default
    # case-insensitive one) is blocked exactly like the real one; the leading
    # "." check on every segment (not just the top one) blanket-blocks any
    # dotfile/dotdir anywhere under a package path, ".cache" included,
    # instead of enumerating every private dotdir by name.
    #
    # Segment checks alone are not enough, though, because they describe the
    # URL rather than the file. A symlink inside out_dir -- `out/Alias ->
    # web` -- gives a URL with no "web" in it, no dot in it, and a target
    # inside out_dir, so every one of those rules says yes and
    # /packages/Alias/uploads/song.mp3 hands over the private original. The
    # second half of the check therefore resolves the path and asks the same
    # questions of where it actually lands. A resolve() per request is real
    # work, and worth it here: this server binds 127.0.0.1 and serves one
    # person, so the traffic is tiny and the file being exposed is their own
    # source audio.
    _BLOCKED_TOPDIRS_CF = {"web", ".cache"}

    def _names_something_private(parts: "tuple[str, ...] | list[str]") -> bool:
        """The single rule for which package paths are off limits, stated
        once and asked twice: of the URL's own segments, and -- after
        resolving -- of the components the path really has.

        Sharing it is the whole point. These were two rules before, and they
        disagreed: the URL half casefolded, while the resolved half compared
        against a literal `out_dir/web`. So on a case-sensitive filesystem
        `/packages/WEB/...` was refused while `out/Alias -> WEB` served the
        very same directory, and one asymmetry in a pair of checks that are
        supposed to mean the same thing is all it takes. Any future rule goes
        here, where both callers get it.
        """
        if not parts:
            return False
        return parts[0].casefold() in _BLOCKED_TOPDIRS_CF or any(
            part.startswith(".") for part in parts
        )

    def _leads_somewhere_private(rest: list[str]) -> bool:
        real_out = out_dir.resolve()
        try:
            real = real_out.joinpath(*rest).resolve()
        except OSError:
            return True  # unresolvable is not something we are willing to serve
        if not real.is_relative_to(real_out):
            return True
        return _names_something_private(real.relative_to(real_out).parts)

    @app.middleware("http")
    async def _block_private_package_paths(request: Request, call_next):
        segments = [s for s in request.url.path.split("/") if s and s != "."]
        if len(segments) >= 2 and segments[0] == "packages":
            rest = segments[1:]
            if (
                ".." in rest
                or _names_something_private(rest)
                or _leads_somewhere_private(rest)
            ):
                return PlainTextResponse("Not Found", status_code=404)
        return await call_next(request)

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html.j2", {})

    @app.post("/api/jobs")
    async def create_job(
        file: UploadFile = File(...),
        title: Optional[str] = Form(None),
        targets: Optional[list[str]] = Form(None),
    ) -> JSONResponse:
        requested_targets = ["guitar"] if targets is None else targets
        if (
            not requested_targets
            or any(not target or target not in REGISTRY for target in requested_targets)
            or len(set(requested_targets)) != len(requested_targets)
        ):
            raise HTTPException(status_code=400, detail="targets must be unique registered values")

        filename = file.filename or ""
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"unsupported file type: {ext or '(none)'}; allowed: "
                + ", ".join(sorted(ALLOWED_EXTENSIONS)),
            )

        try:
            # Not `store.uploads_dir` directly: web/uploads is a fixed,
            # predictable name, so replacing it with a symlink would have
            # every upload land wherever it points. The read side already
            # checks this (JobStore._resolved_upload); this is its mirror,
            # and it runs before a single byte is written.
            uploads_dir = store.verified_uploads_dir()
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"upload directory is not usable, refusing to store the file: {exc}",
            )

        fd, tmp_name = tempfile.mkstemp(dir=uploads_dir, suffix=ext)
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
        dest = uploads_dir / f"{digest}{ext}"
        if dest.exists():
            tmp_path.unlink(missing_ok=True)  # already have this exact content on disk
        else:
            os.replace(tmp_path, dest)  # atomic rename on the same filesystem

        requested_title = (title or "").strip() or Path(filename).stem or "untitled"
        results = store.create_jobs(dest, digest, requested_title, requested_targets)
        jobs = [
            {"id": job.id, "target": job.target, "dedup": not created}
            for job, created in results
        ]
        all_dedup = all(not created for _, created in results)

        return JSONResponse(
            {"job_id": results[0][0].id, "dedup": all_dedup, "jobs": jobs},
            status_code=200 if all_dedup else 202,
        )

    @app.get("/api/jobs")
    def list_jobs() -> list[dict]:
        return [_serialize_job(j) for j in store.list_jobs()]

    @app.get("/api/songs")
    def list_songs() -> list[dict]:
        return [
            _serialize_song(
                song,
                store.latest_pocket_job(song.targets[0].digest) if song.targets else None,
            )
            for song in store.list_songs()
        ]

    @app.get("/api/pocket/status")
    def pocket_status() -> dict:
        try:
            config = read_config(out_dir)
        except (OSError, ValueError):
            return {
                "connected": False,
                "state": "unknown",
                "message": "Pocket の接続設定を確認できません。",
                "target_count": 0,
                "songs": [],
            }
        if config is None:
            return {"connected": False, "target_count": 0, "songs": []}
        client = PocketHTTPClient(config.base_url, config.token)
        packages = inspect_packages(out_dir, client)
        digest_to_web_id = {
            song.targets[0].digest: song.id
            for song in store.list_songs()
            if song.targets
        }
        safe_to_web_id = {}
        for song in store.list_songs():
            package_name = next(
                (
                    Path(job.package).parent.name
                    for job in song.targets
                    if job.package is not None
                ),
                safe_filename(song.title),
            )
            safe_to_web_id[package_name] = song.id
        songs = [
            {
                "web_song_id": (
                    digest_to_web_id.get(item.digest)
                    if item.digest is not None
                    else safe_to_web_id.get(item.safe_name)
                ),
                "song_id": item.song_id,
                "title": item.title,
                "safe_name": item.safe_name,
                "state": item.remote.state,
                "can_sync": item.remote.can_sync,
                "message": item.remote.message,
                "conflict": item.remote.conflict,
            }
            for item in packages
        ]
        status_job = store.active_pocket_job() or store.latest_finished_pocket_all_job()
        return {
            "connected": True,
            "target_count": sum(item.song_id is not None for item in packages),
            "package_count": len(packages),
            "job": _serialize_job(status_job) if status_job is not None else None,
            "songs": songs,
        }

    def _pocket_config_or_409():
        try:
            config = read_config(out_dir)
        except (OSError, ValueError):
            raise HTTPException(status_code=409, detail="Pocket の接続設定を確認できません。")
        if config is None:
            raise HTTPException(status_code=409, detail="Pocket の接続設定がありません。")
        return config

    @app.post("/api/pocket/sync/{pocket_song_id}")
    def create_pocket_sync(pocket_song_id: str) -> JSONResponse:
        _pocket_config_or_409()
        if re.fullmatch(r"[0-9a-f]{12}", pocket_song_id) is None:
            raise HTTPException(status_code=400, detail="song ID が不正です。")
        try:
            sync_lock = SyncLock(out_dir).acquire()
        except (OSError, SyncLockBusy) as exc:
            raise HTTPException(status_code=409, detail=safe_error(exc))
        try:
            package = resolve_package(out_dir, pocket_song_id, include_original=True)
            job = store.create_pocket_job(
                song_id=pocket_song_id,
                digest=package.metadata.source.digest,
                safe_name=package.directory.name,
                sync_lock=sync_lock,
            )
        except PocketServiceError as exc:
            sync_lock.release()
            status = 404 if exc.kind == "not_found" else 409
            raise HTTPException(status_code=status, detail=safe_error(exc))
        except SyncLockBusy as exc:
            raise HTTPException(status_code=409, detail=safe_error(exc))
        except BaseException:
            sync_lock.release()
            raise
        return JSONResponse({"job_id": job.id}, status_code=202)

    @app.post("/api/pocket/sync")
    def create_pocket_sync_all() -> JSONResponse:
        _pocket_config_or_409()
        try:
            sync_lock = SyncLock(out_dir).acquire()
        except (OSError, SyncLockBusy) as exc:
            raise HTTPException(status_code=409, detail=safe_error(exc))
        try:
            job = store.create_pocket_job(
                song_id=None,
                digest=None,
                safe_name=None,
                sync_lock=sync_lock,
            )
        except SyncLockBusy as exc:
            raise HTTPException(status_code=409, detail=safe_error(exc))
        except BaseException:
            sync_lock.release()
            raise
        return JSONResponse({"job_id": job.id}, status_code=202)

    @app.delete("/api/songs/{song_id}", status_code=204)
    def delete_song(song_id: str) -> Response:
        try:
            store.delete_song(song_id)
        except SongNotFoundError:
            raise HTTPException(status_code=404, detail="song not found")
        except SongDeleteConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"song deletion failed: {exc}")
        return Response(status_code=204)

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
