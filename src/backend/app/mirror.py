"""Keep the working folder (config.STORAGE_DIR) and the object store in step, request by request.

The rest of the backend reads and writes ordinary files. On a serverless host those files vanish between requests, so
around every request this module (1) pulls what the request needs from the bucket, (2) lets the handler work on local
files as usual, and (3) pushes whatever the handler created or changed and removes whatever it deleted.

What is pulled depends on the request, because a cold start must stay fast:
* always: the evidence index, id counter, case settings, calibrations and the latest results (small);
* writes also pull the audit log (append-only, so it must not be overwritten by an empty copy);
* analysis, frames, calibration and deletes also pull the evidence videos and their cached model answers;
* run history and the audit view pull theirs on demand.

Disabled (no bucket configured), nothing here runs and the backend behaves exactly as before, on local disk.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from app import config, store

log = logging.getLogger(__name__)

STATE_KEYS = ("evidence_index.json", "evidence_counter.txt", "incident.json")
STATE_PREFIXES = ("calibration/", "runs/latest/")


class Mirror:
    def __init__(self, objects):
        self.objects = objects
        self.etag: dict[str, str] = {}      # key -> etag of what this process last pulled or pushed
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return config.STORAGE_DIR

    # -------------------------------------------------------------- pull

    def _sync_down(self, prefix: str, *, delete_missing: bool = False) -> None:
        remote = {o.key: o for o in self.objects.list(prefix)}
        for key, o in remote.items():
            local = self.root / key
            if local.exists() and self.etag.get(key) == o.etag:
                continue
            data = self.objects.get(key)
            if data is None:
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            self.etag[key] = o.etag
        if delete_missing:  # deleted elsewhere: forget it here too
            base = self.root / prefix
            for p in (base.rglob("*") if base.is_dir() else []):
                key = p.relative_to(self.root).as_posix()
                if p.is_file() and key not in remote:
                    p.unlink()
                    self.etag.pop(key, None)

    def pull(self, method: str, path: str) -> None:
        with self._lock:
            for key in STATE_KEYS:
                self._sync_down(key)
            for prefix in STATE_PREFIXES:
                self._sync_down(prefix, delete_missing=True)
            if method != "GET" or path.startswith("/api/audit"):
                self._sync_down("audit.jsonl")
            self._apply_case()
            if path.startswith("/api/runs"):
                self._sync_down("runs/")
            if self._needs_files(method, path):
                for e in store.list_evidence():
                    self._sync_down(f"uploads/{Path(e.stored_path).name}")
                    if e.sha256:
                        self._sync_down(f"cache/vision/{e.sha256[:16]}/")

    @staticmethod
    def _needs_files(method: str, path: str) -> bool:
        return (path.startswith("/api/analysis") or path.startswith("/api/report")
                or (path.startswith("/api/evidence") and (method == "DELETE" or "/frames/" in path or "/calibration" in path)))

    def _apply_case(self) -> None:
        """Case settings live in memory; make them the saved ones (another instance may have changed them)."""
        f = config.incident_file()
        if f.exists():
            try:
                config.apply_incident(json.loads(f.read_text()))
            except (ValueError, OSError):
                log.warning("saved case settings could not be applied")

    # -------------------------------------------------------------- push

    def snapshot(self) -> dict[str, tuple[int, int]]:
        out = {}
        for p in self.root.rglob("*") if self.root.exists() else []:
            if p.is_file():
                st = p.stat()
                out[p.relative_to(self.root).as_posix()] = (st.st_mtime_ns, st.st_size)
        return out

    def commit(self, before: dict[str, tuple[int, int]]) -> None:
        after = self.snapshot()
        with self._lock:
            for key, sig in after.items():
                if before.get(key) != sig:
                    self.etag[key] = self.objects.put(key, (self.root / key).read_bytes())
            for key in before.keys() - after.keys():
                self.objects.delete(key)
                self.etag.pop(key, None)


_mirror: Mirror | None = None
_loaded = False


def get() -> Mirror | None:
    """The process-wide mirror, or None when no bucket is configured."""
    global _mirror, _loaded
    if not _loaded:
        from app import objectstore
        objects = objectstore.from_env()
        _mirror = Mirror(objects) if objects else None
        _loaded = True
    return _mirror


def use(objects) -> None:
    """Install an object store (tests); None switches mirroring off."""
    global _mirror, _loaded
    _mirror, _loaded = (Mirror(objects) if objects else None), True
