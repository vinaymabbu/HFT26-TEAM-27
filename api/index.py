"""Vercel entrypoint: serves the FastAPI app (every /api/* request is rewritten here, see vercel.json)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "backend"))
os.environ.setdefault("STORAGE_DIR", "/tmp/storage")   # the only writable disk; durable state lives in the R2 bucket

from app.main import app  # noqa: E402,F401
