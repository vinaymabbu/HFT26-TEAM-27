"""Serverless hosting: state must survive between requests that run on different, empty machines.

A "cold start" is simulated by pointing the backend at a fresh, empty working folder; the object store (here a folder
standing in for the bucket) is the only thing the instances share.
"""
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, mirror
from app.jobs import jobs
from app.main import app
from app.objectstore import LocalObjectStore, R2ObjectStore, presign_url, sign_headers

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    store = LocalObjectStore(tmp_path / "bucket")
    mirror.use(store)
    monkeypatch.setattr(config, "FEATHERLESS_API_KEY", "")
    yield store
    mirror.use(None)


def cold_start(tmp_path, monkeypatch, name):
    """A new serverless instance: empty working folder, nothing remembered in memory."""
    old = config.STORAGE_DIR
    if old.parent == tmp_path and old.exists():
        shutil.rmtree(old)                      # the previous machine is gone, with everything on its disk
    d = tmp_path / name
    monkeypatch.setattr(config, "STORAGE_DIR", d)
    monkeypatch.setattr(config, "UPLOAD_DIR", d / "uploads")
    monkeypatch.setattr(config, "RUN_DIR", d / "runs" / "latest")
    mirror.get().etag.clear()
    return TestClient(app)


def _sensor(client, name):
    r = client.post("/api/evidence/sensor", files={"file": (name, (ROOT / "data" / "sensor" / name).read_bytes())})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ the state round trip

def test_evidence_results_and_case_settings_survive_cold_starts(bucket, tmp_path, monkeypatch):
    a = cold_start(tmp_path, monkeypatch, "instance_a")
    case = json.loads((ROOT / "data" / "case.json").read_text())
    assert a.put("/api/incident", json=case).status_code == 200
    ids = [_sensor(a, f)["ref"] for f in ("signal_controller.csv", "v1_telematics.csv", "v2_obd.csv")]
    assert ids == ["EVID-001", "EVID-002", "EVID-003"]
    run = a.post("/api/analysis/run").json()
    assert {s["stage"]: s["status"] for s in run["stages"]}["3. timeline"] == "real"

    monkeypatch.setattr(config, "INCIDENT_ID", "FORGOTTEN")          # a new process knows nothing
    b = cold_start(tmp_path, monkeypatch, "instance_b")
    assert b.get("/api/incident").json()["incident_id"] == case["incident_id"]       # case settings come back
    assert [e["ref"] for e in b.get("/api/evidence").json()] == ids                   # evidence index comes back
    assert b.get("/api/analysis/latest").json()["run_id"] == run["run_id"]            # so do the results
    assert len(b.get("/api/timeline").json()["events"]) > 5 and b.get("/api/conflicts").json()["conflicts"]

    # a re-run on yet another instance needs the uploaded files, fetched from the bucket on demand
    c = cold_start(tmp_path, monkeypatch, "instance_c")
    again = c.post("/api/analysis/run").json()
    assert {s["stage"]: s["status"] for s in again["stages"]}["5. reconstruction"] == "real"
    assert [e["ref"] for e in c.get("/api/evidence").json()] == ids                   # numbering continues, no gaps


def test_deleting_evidence_removes_it_everywhere_and_numbers_are_not_reused(bucket, tmp_path, monkeypatch):
    a = cold_start(tmp_path, monkeypatch, "a")
    first = _sensor(a, "v1_telematics.csv")
    assert any(o.key.startswith("uploads/") for o in bucket.list())
    b = cold_start(tmp_path, monkeypatch, "b")
    assert b.delete(f"/api/evidence/{first['id']}").status_code == 204
    assert not any(o.key.startswith("uploads/") for o in bucket.list())               # the stored copy is gone from the bucket
    c = cold_start(tmp_path, monkeypatch, "c")
    assert c.get("/api/evidence").json() == []
    assert _sensor(c, "v2_obd.csv")["ref"] == "EVID-002"                              # EVID-001 is never reissued


def test_the_audit_log_is_pulled_before_it_is_appended_to(bucket, tmp_path, monkeypatch):
    a = cold_start(tmp_path, monkeypatch, "a")
    _sensor(a, "v1_telematics.csv")
    b = cold_start(tmp_path, monkeypatch, "b")
    _sensor(b, "v2_obd.csv")                      # an empty local audit file must not overwrite the first entry
    events = [e["event"] for e in cold_start(tmp_path, monkeypatch, "c").get("/api/audit").json()]
    assert events.count("evidence_uploaded") == 2


def test_nothing_is_mirrored_without_a_bucket(tmp_path):
    mirror.use(None)
    client = TestClient(app)
    assert client.get("/api/health").json()["direct_upload"] is False
    assert client.post("/api/evidence/presign", json={"filename": "x.mp4"}).status_code == 501


# ------------------------------------------------------------------ uploads that bypass the request-size limit

def test_a_large_video_goes_straight_to_the_bucket_and_is_registered(bucket, tmp_path, monkeypatch):
    from synthetic import render_cctv
    a = cold_start(tmp_path, monkeypatch, "a")
    assert a.get("/api/health").json()["direct_upload"] is True
    video = render_cctv(tmp_path / "v.avi", [], duration_s=1.0)

    slot = a.post("/api/evidence/presign", json={"filename": "crash.avi", "size_bytes": video.stat().st_size}).json()
    assert slot["key"].startswith("incoming/") and slot["method"] == "PUT" and slot["kind"] == "video"
    bucket.put(slot["key"], video.read_bytes())                                       # what the browser does with the URL

    b = cold_start(tmp_path, monkeypatch, "b")                                        # registered on another instance
    r = b.post("/api/evidence/register", json={"key": slot["key"], "filename": "crash.avi"})
    assert r.status_code == 201 and r.json()["kind"] == "video" and r.json()["details"]["width"] == 480
    assert r.json()["sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
    keys = [o.key for o in bucket.list()]
    assert not any(k.startswith("incoming/") for k in keys)                           # staging copy removed
    assert any(k.startswith("uploads/") for k in keys)                                # validated copy kept as evidence


def test_register_refuses_foreign_keys_missing_uploads_and_unreadable_files(bucket, tmp_path, monkeypatch):
    a = cold_start(tmp_path, monkeypatch, "a")
    assert a.post("/api/evidence/register", json={"key": "state/evidence_index.json", "filename": "x.csv"}).status_code == 400
    assert a.post("/api/evidence/register", json={"key": "incoming/../x", "filename": "x.csv"}).status_code == 400
    assert a.post("/api/evidence/register", json={"key": "incoming/none/x.mp4", "filename": "x.mp4"}).status_code == 404
    bucket.put("incoming/k/bad.mp4", b"this is not a video")
    r = a.post("/api/evidence/register", json={"key": "incoming/k/bad.mp4", "filename": "bad.mp4"})
    assert r.status_code == 422 and not [o for o in bucket.list() if o.key.startswith("uploads/")]
    assert a.post("/api/evidence/presign", json={"filename": "notes.docx"}).status_code == 415


# ------------------------------------------------------------------ analysis inside the request

def test_with_inline_jobs_the_response_is_the_finished_job(bucket, tmp_path, monkeypatch):
    monkeypatch.setattr("app.jobs.INLINE", True)
    monkeypatch.setattr(jobs, "_job", None)
    a = cold_start(tmp_path, monkeypatch, "a")
    a.put("/api/incident", json=json.loads((ROOT / "data" / "case.json").read_text()))
    for f in ("signal_controller.csv", "v1_telematics.csv", "v2_obd.csv"):
        _sensor(a, f)
    job = a.post("/api/analysis/jobs").json()
    assert job["state"] == "done" and job["run"]["run_id"] and job["finished_at"]
    assert cold_start(tmp_path, monkeypatch, "b").get("/api/analysis/latest").json()["run_id"] == job["run"]["run_id"]


# ------------------------------------------------------------------ the S3/R2 client

def test_signatures_match_the_published_aws_examples():
    when = datetime(2013, 5, 24, tzinfo=timezone.utc)
    key, secret = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    url = presign_url("GET", "examplebucket.s3.amazonaws.com", "/test.txt", key, secret, "us-east-1", expires=86400, now=when)
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
    h = sign_headers("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {}, hashlib.sha256(b"").hexdigest(), key, secret,
                     "us-east-1", now=when, extra={"Range": "bytes=0-9"})
    assert h["authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def _r2(handler):
    store = R2ObjectStore("acct", "bkt", "AK", "SK")
    store._http = httpx.Client(transport=httpx.MockTransport(handler))
    return store


def test_r2_client_speaks_s3():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, dict(req.url.params)))
        assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AK/") and "x-amz-content-sha256" in req.headers
        if req.method == "GET" and req.url.path == "/bkt/missing":
            return httpx.Response(404)
        if req.method == "GET" and req.url.path == "/bkt/a.json":
            return httpx.Response(200, content=b"{}")
        if req.method == "PUT":
            return httpx.Response(200, headers={"etag": '"abc123"'})
        if req.method == "DELETE":
            return httpx.Response(204)
        page2 = req.url.params.get("continuation-token") == "t1"
        body = ("<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>"
                + ("<Contents><Key>p/b</Key><Size>5</Size><ETag>&quot;e2&quot;</ETag></Contents><IsTruncated>false</IsTruncated>" if page2 else
                   "<Contents><Key>p/a</Key><Size>3</Size><ETag>&quot;e1&quot;</ETag></Contents><IsTruncated>true</IsTruncated>"
                   "<NextContinuationToken>t1</NextContinuationToken>") + "</ListBucketResult>")
        return httpx.Response(200, content=body)

    s = _r2(handler)
    assert s.get("missing") is None and s.get("a.json") == b"{}"
    assert s.put("x/y.bin", b"data") == "abc123"
    s.delete("x/y.bin")
    assert [(o.key, o.size, o.etag) for o in s.list("p/")] == [("p/a", 3, "e1"), ("p/b", 5, "e2")]     # follows pagination
    url = s.presign_put("incoming/u/v.mp4")
    assert url.startswith("https://acct.r2.cloudflarestorage.com/bkt/incoming/u/v.mp4?") and "X-Amz-Signature=" in url


def test_the_local_store_refuses_keys_that_escape_it(tmp_path):
    s = LocalObjectStore(tmp_path / "b")
    with pytest.raises(ValueError):
        s.put("../outside", b"x")
