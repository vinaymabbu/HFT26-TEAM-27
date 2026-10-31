"""Vercel entrypoint: serves the FastAPI app.

vercel.json rewrites /api/<rest> to this function with the original path in `__p`, because the function itself is
invoked as /api/index; the real path is restored here before FastAPI routes it.
"""
import os
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "backend"))
os.environ.setdefault("STORAGE_DIR", "/tmp/storage")   # the only writable disk; durable state lives in the R2 bucket

from app.main import app as _app  # noqa: E402


async def app(scope, receive, send):
    if scope["type"] == "http" and scope["path"].startswith("/api/index"):
        pairs = parse_qsl(scope.get("query_string", b"").decode(), keep_blank_values=True)
        rest = next((v for k, v in pairs if k == "__p"), "")
        path = "/api/" + rest.lstrip("/")
        scope = {**scope, "path": path, "raw_path": path.encode(),
                 "query_string": urlencode([(k, v) for k, v in pairs if k != "__p"]).encode()}
    await _app(scope, receive, send)
