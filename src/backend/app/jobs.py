"""Background analysis jobs: one at a time, polled by the dashboard for live progress."""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timezone

from app import pipeline
from app.schemas import AnalysisJob, StageProgress, StageResult

log = logging.getLogger(__name__)

# On Vercel (or INLINE_JOBS=1) analysis runs inside the request instead of in a background thread.
INLINE = bool(os.environ.get("VERCEL") or os.environ.get("INLINE_JOBS"))


class _JobTracker:
    """pipeline.Tracker that writes into the current AnalysisJob."""

    def __init__(self, job: AnalysisJob, lock: threading.Lock):
        self.job, self.lock = job, lock

    def _stage(self, name: str) -> StageProgress:
        for s in self.job.stages:
            if s.stage == name:
                return s
        s = StageProgress(stage=name)
        self.job.stages.append(s)
        return s

    def plan(self, stages: list[str]) -> None:
        with self.lock:
            self.job.stages = [StageProgress(stage=s) for s in stages]

    def start(self, stage: str) -> None:
        with self.lock:
            self._stage(stage).status = "running"

    def update(self, stage: str, done: int, total: int, note: str) -> None:
        with self.lock:
            s = self._stage(stage)
            s.done, s.total, s.note = done, total, note

    def finish(self, result: StageResult) -> None:
        with self.lock:
            s = self._stage(result.stage)
            s.status, s.note = result.status, result.message or s.note
            if s.total:
                s.done = s.total if result.status == "real" else s.done


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._job: AnalysisJob | None = None

    def current(self) -> AnalysisJob | None:
        with self._lock:
            return self._job.model_copy(deep=True) if self._job else None

    def start(self) -> AnalysisJob:
        """Start a run in the background, or return the one already running."""
        with self._lock:
            if self._job and self._job.state == "running":
                return self._job.model_copy(deep=True)
            job = AnalysisJob(job_id=uuid.uuid4().hex[:8], state="running",
                              started_at=datetime.now(timezone.utc), stages=[])
            self._job = job
        if INLINE:  # serverless: a function is frozen once it has responded, so the run must finish inside the request
            self._work(job)
        else:
            threading.Thread(target=self._work, args=(job,), name=f"analysis-{job.job_id}", daemon=True).start()
        return self.current()

    def _work(self, job: AnalysisJob) -> None:
        tracker = _JobTracker(job, self._lock)
        try:
            run = pipeline.run_analysis(tracker)
            with self._lock:
                job.run, job.state = run, "done"
        except Exception as e:  # RunInProgress (a synchronous run holds the lock) or a pipeline bug
            log.exception("analysis job %s failed", job.job_id)
            with self._lock:
                job.state, job.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                job.finished_at = datetime.now(timezone.utc)


jobs = JobManager()
