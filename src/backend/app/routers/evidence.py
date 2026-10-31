"""Evidence upload, validation and storage (feeds modules 1–2)."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2
from fastapi import APIRouter, HTTPException, Response, UploadFile
from pydantic import BaseModel

from app import config, mirror, store
from app.modules.extraction.sensor import inspect_csv
from app.modules.ingestion import calibration
from app.modules.ingestion import calibration_key
from app.modules.ingestion.video import VideoError, probe_video
from app.schemas import CalibrationPoints, Evidence, SourceType

MAX_TEXT_CHARS = 200_000

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


def _kind_for(filename: str) -> SourceType:
    ext = Path(filename).suffix.lower()
    for kind, exts in config.ALLOWED_EXTENSIONS.items():
        if ext in exts:
            return SourceType(kind)
    allowed = sorted(e for exts in config.ALLOWED_EXTENSIONS.values() for e in exts)
    raise HTTPException(415, f"Unsupported file type '{ext}'. Allowed: {', '.join(allowed)}")


class PresignRequest(BaseModel):
    filename: str
    size_bytes: int = 0


class RegisterRequest(BaseModel):
    key: str
    filename: str


@router.post("/presign")
def presign_upload(req: PresignRequest) -> dict:
    """A one-hour URL the browser can PUT a (large) file to directly, bypassing the function's request-size limit.
    Then call /register. Needs an object store (see mirror.py)."""
    m = mirror.get()
    if m is None:
        raise HTTPException(501, "Direct uploads need an object store (R2_* settings); use the normal upload instead")
    kind = _kind_for(req.filename)  # reject unsupported types before anything is uploaded
    if req.size_bytes > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large")
    key = f"incoming/{uuid.uuid4().hex}/{Path(req.filename).name}"
    return {"key": key, "url": m.objects.presign_put(key), "method": "PUT", "kind": kind.value}


@router.post("/register", response_model=Evidence, status_code=201)
def register_upload(req: RegisterRequest) -> Evidence:
    """Take a file the browser put in the bucket (via /presign), validate it like any upload and store it as evidence."""
    m = mirror.get()
    if m is None:
        raise HTTPException(501, "Direct uploads need an object store")
    if not req.key.startswith("incoming/") or ".." in req.key:
        raise HTTPException(400, "Not an upload key")
    data = m.objects.get(req.key)
    if data is None:
        raise HTTPException(404, "Nothing was uploaded under that key (did the PUT finish?)")
    try:
        return _ingest_bytes(req.filename, data, None, _kind_for(req.filename))
    finally:
        m.objects.delete(req.key)  # the validated copy is stored as evidence; the staging object is not kept


@router.get("", response_model=list[Evidence])
def list_evidence() -> list[Evidence]:
    return store.list_evidence()


@router.post("", response_model=Evidence, status_code=201)
async def upload_evidence(file: UploadFile) -> Evidence:
    """Any supported file; the kind (video / witness / sensor) is taken from the extension."""
    return await _ingest(file, None)


@router.post("/video", response_model=Evidence, status_code=201)
async def upload_video(file: UploadFile) -> Evidence:
    """Dashcam / CCTV footage. Must be a readable video."""
    return await _ingest(file, SourceType.video)


@router.post("/witness", response_model=Evidence, status_code=201)
async def upload_witness(file: UploadFile) -> Evidence:
    """A written witness statement: a non-empty UTF-8 .txt file."""
    return await _ingest(file, SourceType.witness)


@router.post("/sensor", response_model=Evidence, status_code=201)
async def upload_sensor(file: UploadFile) -> Evidence:
    """A sensor record: a .csv with a timestamp column plus speed, position, signal or motion columns."""
    return await _ingest(file, SourceType.sensor)


async def _ingest(file: UploadFile, expected: SourceType | None) -> Evidence:
    if not file.filename:
        raise HTTPException(400, "Missing filename")
    kind = _kind_for(file.filename)
    if expected is not None and kind != expected:
        raise HTTPException(415, f"{file.filename}: this endpoint takes {expected.value} evidence, but the file looks like {kind.value}")
    return _ingest_bytes(file.filename, await file.read(), file.content_type, kind)


def _ingest_bytes(filename: str, data: bytes, content_type: str | None, kind: SourceType) -> Evidence:
    """Validate and store one file's bytes as evidence (shared by the upload endpoints and direct-to-bucket uploads)."""
    file = type("F", (), {"filename": filename, "content_type": content_type})
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large")

    sha256 = hashlib.sha256(data).hexdigest()
    if dup := next((e for e in store.list_evidence() if e.sha256 == sha256), None):
        store.audit("evidence_rejected", filename=file.filename, sha256=sha256, reason=f"duplicate of {dup.id}")
        raise HTTPException(409, f"Duplicate: identical to {dup.id} ({dup.filename}), uploaded {dup.uploaded_at:%Y-%m-%d %H:%M} UTC")

    evidence_id = f"ev_{uuid.uuid4().hex[:8]}"
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.UPLOAD_DIR / f"{evidence_id}{Path(file.filename).suffix.lower()}"
    dest.write_bytes(data)

    try:
        details = _inspect(kind, dest, file.filename, data)
    except ValueError as e:  # unusable file: say why now, not halfway through a run
        dest.unlink(missing_ok=True)
        store.audit("evidence_rejected", filename=file.filename, sha256=sha256, reason=str(e))
        raise HTTPException(422, str(e))

    evidence = Evidence(
        id=evidence_id, kind=kind, filename=file.filename, content_type=file.content_type,
        size_bytes=len(data), stored_path=str(dest), uploaded_at=datetime.now(timezone.utc),
        sha256=sha256, details=details,
    )
    store.add_evidence(evidence)
    store.audit("evidence_uploaded", id=evidence_id, filename=file.filename, kind=kind.value,
                size_bytes=len(data), sha256=sha256, details=details)
    return evidence


def _inspect(kind: SourceType, path: Path, filename: str, data: bytes) -> dict:
    """Check the file is usable for its kind; return what was learned. Raises ValueError."""
    if kind == SourceType.video:
        try:
            info = probe_video(str(path))
        except VideoError as e:
            raise ValueError(f"{filename}: not a readable video ({e})") from e
        return {"fps": round(info.fps, 3), "frames": info.frame_count, "duration_s": round(info.duration_s, 2),
                "width": info.width, "height": info.height}
    if kind == SourceType.sensor:
        try:
            return inspect_csv(str(path), filename)
        except ValueError:
            raise
        except Exception as e:  # malformed CSV
            raise ValueError(f"{filename}: could not be read as CSV ({type(e).__name__}: {e})") from e
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{filename}: witness statements must be UTF-8 text") from e
    if not text.strip():
        raise ValueError(f"{filename}: the statement is empty")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"{filename}: statement longer than {MAX_TEXT_CHARS} characters")
    return {"lines": len(text.splitlines()), "words": len(text.split())}


@router.delete("/{evidence_id}", status_code=204)
def delete_evidence(evidence_id: str) -> None:
    ev = next((e for e in store.list_evidence() if e.id == evidence_id), None)
    if not store.delete_evidence(evidence_id):
        raise HTTPException(404, "Evidence not found")
    store.audit("evidence_deleted", id=evidence_id, filename=ev.filename, sha256=ev.sha256)


@router.get("/{evidence_id}/frames/{index}", response_class=Response,
            responses={200: {"content": {"image/jpeg": {}}}})
def video_frame(evidence_id: str, index: int, width: int = 480) -> Response:
    """One frame of an uploaded video as a JPEG (thumbnails for `frame:<n>` source refs)."""
    ev = next((e for e in store.list_evidence() if e.id == evidence_id), None)
    if ev is None or ev.kind != SourceType.video:
        raise HTTPException(404, "Video evidence not found")
    cap = cv2.VideoCapture(ev.stored_path)
    try:
        if index < 0 or index >= int(cap.get(cv2.CAP_PROP_FRAME_COUNT)):
            raise HTTPException(404, "Frame out of range")
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, image = cap.read()
    finally:
        cap.release()
    if not ok:
        raise HTTPException(500, "Could not decode frame")
    width = max(64, min(width, 1280))
    h, w = image.shape[:2]
    if w > width:
        image = cv2.resize(image, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


def _video(evidence_id: str) -> Evidence:
    ev = next((e for e in store.list_evidence() if e.id == evidence_id), None)
    if ev is None or ev.kind != SourceType.video:
        raise HTTPException(404, "Video evidence not found")
    return ev


@router.get("/{evidence_id}/calibration")
def get_calibration(evidence_id: str) -> dict:
    """Per camera view: the manual calibration (if set) and the automatic one from the last run."""
    return calibration.load(calibration_key(_video(evidence_id)))


@router.put("/{evidence_id}/calibration/{view}")
def set_calibration(evidence_id: str, view: int, body: CalibrationPoints) -> dict:
    """Manual calibration of one view (overrides the automatic one). Re-run the analysis to apply it."""
    ev = _video(evidence_id)
    try:
        cal = calibration.manual_calibration(view, [list(p) for p in body.image_points],
                                             [list(p) for p in body.ground_points])
    except ValueError as e:
        raise HTTPException(422, str(e))
    data = calibration.load(calibration_key(ev))
    data.setdefault("manual", {})[str(view)] = cal.to_dict() | {"note": body.note}
    calibration.save(calibration_key(ev), data)
    store.audit("calibration_set", id=evidence_id, filename=ev.filename, sha256=ev.sha256, view=view,
                points=len(body.image_points), reprojection_m=cal.reprojection_m, note=body.note)
    return data["manual"][str(view)]


@router.delete("/{evidence_id}/calibration/{view}", status_code=204)
def delete_calibration(evidence_id: str, view: int) -> None:
    ev = _video(evidence_id)
    data = calibration.load(calibration_key(ev))
    if data.get("manual", {}).pop(str(view), None) is None:
        raise HTTPException(404, "No manual calibration for this view")
    calibration.save(calibration_key(ev), data)
    store.audit("calibration_removed", id=evidence_id, filename=ev.filename, sha256=ev.sha256, view=view)
