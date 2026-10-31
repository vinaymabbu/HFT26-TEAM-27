"""Vercel entrypoint: serves the FastAPI app and the built dashboard.

vercel.json rewrites /api/<rest> to this function with the original path in `__p`, because the function itself is
invoked as /api/index; the real path is restored here before FastAPI routes it. Any other path is the dashboard
(src/frontend/dist, bundled with the function), falling back to index.html for the hash-routed single page.
"""
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "backend"))
os.environ.setdefault("STORAGE_DIR", "/tmp/storage")   # the only writable disk; durable state lives in the R2 bucket

from starlette.responses import FileResponse, PlainTextResponse  # noqa: E402
from app.main import app as _app  # noqa: E402

DIST = ROOT / "src" / "frontend" / "dist"


async def _static(scope, receive, send):
    rel = scope["path"].lstrip("/")
    f = (DIST / rel).resolve()
    if not (rel and f.is_file() and DIST.resolve() in f.parents):
        f = DIST / "index.html"
    resp = FileResponse(f) if f.is_file() else PlainTextResponse("dashboard not built", status_code=404)
    await resp(scope, receive, send)


async def app(scope, receive, send):
    if scope["type"] != "http":
        return await _app(scope, receive, send)
    path = scope["path"]
    if path.startswith("/api/index"):
        pairs = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
        rest = next((v for k, v in pairs if k == "__p"), "")
        path = "/api/" + rest.lstrip("/")
        scope = {**scope, "path": path, "raw_path": path.encode(),
                 "query_string": urlencode([(k, v) for k, v in pairs if k != "__p"]).encode()}
    if path.startswith("/api"):
        return await _app(scope, receive, send)
    await _static(scope, receive, send)
