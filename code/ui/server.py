"""FastAPI prefab UI — shells out to flow.py, reads persistence only."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from comparison_report import build_report, generate_report
from gateway import GATEWAY_URL
from persistence import SESSIONS_ROOT, SessionStore, delete_all_sessions, delete_session, list_sessions, validate_session_id

UI_DIR = Path(__file__).resolve().parent
CODE_DIR = UI_DIR.parent
QUERIES_PATH = UI_DIR / "queries.json"
STATIC_DIR = UI_DIR / "static"

SESSION_LINE = re.compile(r"^session\s+(\S+)\s+[─\-]", re.MULTILINE)

app = FastAPI(title="Web Agent UI")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _load_queries() -> list[dict[str, str]]:
    return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))


def _query_by_id(query_id: str) -> dict[str, str]:
    for q in _load_queries():
        if q["id"] == query_id:
            return q
    raise HTTPException(status_code=404, detail=f"Unknown query id: {query_id}")


def _session_summary(sid: str) -> dict[str, Any]:
    store = SessionStore(sid)
    query = store.read_query() or ""
    report_path = store.dir / "REPORT.html"
    created = modified = 0.0
    try:
        stat = store.dir.stat()
        # st_ctime is creation time on Windows; fall back to mtime elsewhere.
        created = getattr(stat, "st_ctime", stat.st_mtime)
        modified = stat.st_mtime
    except OSError:
        pass
    return {
        "session_id": sid,
        "query": query[:200],
        "has_report": report_path.is_file(),
        "created_at": created,
        "modified_at": modified,
    }


def _safe_session_path(sid: str, *parts: str) -> Path:
    base = (SESSIONS_ROOT / sid).resolve()
    target = base.joinpath(*parts).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    return target


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/gateway/status")
async def gateway_status() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{GATEWAY_URL}/v1/routers")
        online = r.status_code == 200
        return {"online": online, "url": GATEWAY_URL, "status_code": r.status_code}
    except Exception as exc:
        return {"online": False, "url": GATEWAY_URL, "error": str(exc)}


@app.get("/api/queries")
async def api_queries() -> list[dict[str, str]]:
    return _load_queries()


@app.get("/api/sessions")
async def api_sessions() -> list[dict[str, Any]]:
    sessions = list_sessions()
    sessions.sort(key=lambda s: (SESSIONS_ROOT / s).stat().st_mtime if (SESSIONS_ROOT / s).exists() else 0, reverse=True)
    return [_session_summary(sid) for sid in sessions]


@app.delete("/api/sessions/{sid}")
async def api_delete_session(sid: str) -> dict[str, Any]:
    try:
        validate_session_id(sid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not delete_session(sid):
        raise HTTPException(status_code=404, detail="Session not found")
    return {"deleted": True, "session_id": sid}


@app.delete("/api/sessions")
async def api_delete_all_sessions() -> dict[str, Any]:
    count = delete_all_sessions()
    return {"deleted": count}


@app.get("/api/sessions/{sid}/report")
async def api_report_json(sid: str) -> dict[str, Any]:
    store = SessionStore(sid)
    if not store.dir.is_dir():
        raise HTTPException(status_code=404, detail="Session not found")
    return build_report(sid)


@app.get("/api/sessions/{sid}/report.html")
async def api_report_html(sid: str) -> HTMLResponse:
    path = _safe_session_path(sid, "REPORT.html")
    if not path.is_file():
        try:
            generate_report(sid, write_html=True)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report not generated")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/api/report-html/{sid}")
async def api_report_html_legacy(sid: str) -> HTMLResponse:
    """Back-compat alias used by older UI bookmarks."""
    return await api_report_html(sid)


@app.get("/api/sessions/{sid}/artifacts/{file_path:path}")
async def api_artifact(sid: str, file_path: str) -> FileResponse:
    path = _safe_session_path(sid, file_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path)


async def _stream_flow(query_text: str):
    yield f"data: {json.dumps({'type': 'started'})}\n\n"
    cmd = ["uv", "run", "python", "-u", "flow.py", query_text]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(CODE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    assert proc.stdout is not None
    buf = ""
    session_id: str | None = None
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace")
        buf += line
        yield f"data: {json.dumps({'type': 'log', 'line': line})}\n\n"
        m = SESSION_LINE.search(buf)
        if m:
            session_id = m.group(1)
    await proc.wait()
    if session_id:
        try:
            generate_report(session_id, write_html=True)
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': f'report: {exc}'})}\n\n"
    payload = {
        "type": "done",
        "exit_code": proc.returncode,
        "session_id": session_id,
        "report_url": f"/api/sessions/{session_id}/report.html" if session_id else None,
    }
    yield f"data: {json.dumps(payload)}\n\n"


@app.post("/api/run")
async def api_run(body: dict[str, str]) -> StreamingResponse:
    custom = (body.get("query") or "").strip()
    query_id = body.get("query_id") or body.get("id")
    if custom:
        query_text = custom
    elif query_id:
        query_text = _query_by_id(query_id)["query"]
    else:
        raise HTTPException(status_code=400, detail="query_id or query required")
    if len(query_text) > 8000:
        raise HTTPException(status_code=400, detail="query too long (max 8000 characters)")
    return StreamingResponse(
        _stream_flow(query_text),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8090, log_level="info")


if __name__ == "__main__":
    main()
