"""FastAPI entry point. Run from src/backend:  uvicorn app.main:app --reload"""
import os

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from app import config, mirror
from app.routers import analysis, evidence, incident
from app.schemas import SCHEMA_VERSION

app = FastAPI(title="Incident Reconstruction API", version=SCHEMA_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                   *[o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]],  # e.g. the Vercel URL
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(evidence.router)
app.include_router(analysis.router)
app.include_router(incident.router)


@app.middleware("http")
async def mirror_state(request: Request, call_next):
    """Serverless hosting: pull what the request needs from the object store, run it on local files, push the changes."""
    m = mirror.get()
    if m is None or request.url.path == "/api/health" or request.method == "OPTIONS":
        return await call_next(request)
    await run_in_threadpool(m.pull, request.method, request.url.path)
    before = await run_in_threadpool(m.snapshot)
    try:
        return await call_next(request)
    finally:  # also after an error: files written before it failed must not be lost, nor deletions
        await run_in_threadpool(m.commit, before)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "schema_version": SCHEMA_VERSION, "incident_id": config.INCIDENT_ID,
            "direct_upload": mirror.get() is not None}
