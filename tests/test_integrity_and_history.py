"""Chain of custody: hashes, duplicate and bad-file rejection, audit log, run manifest and history."""
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)
DATA = Path(__file__).resolve().parents[1] / "data"


def _upload(name, data, ctype="text/csv"):
    return client.post("/api/evidence", files={"file": (name, data, ctype)})


def test_upload_records_hash_and_inspection():
    data = (DATA / "sensor" / "v2_obd.csv").read_bytes()
    ev = _upload("v2_obd.csv", data).json()
    assert ev["sha256"] == hashlib.sha256(data).hexdigest()
    assert ev["details"]["layout"] == "vehicle_track" and ev["details"]["rows"] > 10


def test_duplicate_and_unusable_files_are_rejected_at_upload():
    data = (DATA / "witness" / "witness_01_pedestrian.txt").read_bytes()
    first = _upload("w1.txt", data, "text/plain")
    dup = _upload("w1-copy.txt", data, "text/plain")
    assert first.status_code == 201 and dup.status_code == 409 and first.json()["id"] in dup.json()["detail"]
    assert _upload("blank.txt", b"   \n", "text/plain").status_code == 422
    assert _upload("bad.txt", b"\xff\xfe\x00bad", "text/plain").status_code == 422
    assert _upload("clip.mp4", b"not a video at all", "video/mp4").status_code == 422
    assert len(client.get("/api/evidence").json()) == 1  # rejected files are not kept


def test_audit_log_and_run_history_with_manifest():
    data = (DATA / "sensor" / "signal_controller.csv").read_bytes()
    ev = _upload("signal_controller.csv", data).json()
    run = client.post("/api/analysis/run").json()
    assert run["evidence"] == [{"id": ev["id"], "filename": "signal_controller.csv",
                                "sha256": hashlib.sha256(data).hexdigest()}]
    assert all(s["duration_s"] is not None for s in run["stages"])
    client.delete(f"/api/evidence/{ev['id']}")

    events = [e["event"] for e in client.get("/api/audit").json()]
    assert events[:4] == ["evidence_deleted", "analysis_finished", "analysis_started", "evidence_uploaded"]

    history = client.get("/api/runs").json()
    assert history[0]["run_id"] == run["run_id"]
    assert client.get(f"/api/runs/{run['run_id']}/timeline").json()["events"]
    assert client.get(f"/api/runs/{run['run_id']}/nope").status_code == 404
    assert client.get("/api/runs/..%2F..%2Fetc/run").status_code == 404


def test_incident_report_markdown_and_json(monkeypatch, demo_case):
    from app import config

    monkeypatch.setattr(config, "FEATHERLESS_API_KEY", "")
    assert client.get("/api/report").status_code == 404  # no run yet
    for name in ("v1_telematics.csv", "v2_obd.csv", "signal_controller.csv"):
        _upload(name, (DATA / "sensor" / name).read_bytes())
    run = client.post("/api/analysis/run").json()

    rep = client.get("/api/report").json()
    assert rep["run_id"] == run["run_id"] and rep["summary"] is None  # no key: no AI narrative
    assert rep["headline"].startswith("Collision between V1 and V2 at t=10 s")
    md = client.get("/api/report?format=markdown")
    assert md.headers["content-type"].startswith("text/markdown")
    text = md.text
    for section in ("## Evidence", "## Timeline", "## Contradictions", "## Evidence gaps", "## Reconstruction",
                    "## Chain of custody"):
        assert section in text
    assert "v1_telematics.csv @ rows:" in text  # every conflict cites its source
    assert client.get(f"/api/runs/{run['run_id']}/report?format=markdown").text.startswith("# Incident report")

    # The report says what is observed and what is inferred, and each event's confidence on observation alone.
    assert "## What is observed and what is inferred" in text and "| Observed alone |" in text
    assert "Reconstruction positions" in text and "Timeline events" in text
    assert rep["facts"]["observed_vs_inferred"]["events"] == len(rep["facts"]["events"])
    assert all("confidence_from_observation_alone" in e for e in rep["facts"]["events"])

    # The readable page: HTML with real tables, a print button and downloads; the Markdown can be fetched as a file.
    page = client.get("/api/report?format=html&narrative=false")
    assert page.headers["content-type"].startswith("text/html")
    assert "<table>" in page.text and "<h2>Timeline</h2>" in page.text and "window.print()" in page.text
    assert "format=markdown&amp;narrative=false&amp;download=true" in page.text
    attach = client.get("/api/report?format=markdown&download=true")
    assert attach.headers["content-disposition"].startswith("attachment") and attach.text.startswith("# Incident report")


def test_the_report_page_escapes_everything_that_came_from_evidence():
    from app.report_html import markdown_to_html, render_page
    hostile = ('# Report <script>alert(1)</script>\n\n| File | Quote |\n|---|---|\n'
               '| <img src=x onerror=alert(1)>.csv | "a" **b** `c<d>` |\n\n- *<b>x</b>* and **<i>y</i>**\n\n> <svg onload=alert(1)>')
    out = markdown_to_html(hostile)
    assert "<script" not in out and "<img" not in out and "<svg" not in out and "<b>" not in out and "<i>" not in out
    assert "&lt;script&gt;" in out and "<table>" in out and "<strong>b</strong>" in out and "<code>c&lt;d&gt;</code>" in out
    page = render_page(hostile, 'run"><script>x</script>', "?a=1&b=2", "?c=3")
    assert "<script>x" not in page and 'run&quot;&gt;&lt;script&gt;' in page


def test_incident_details_drive_times_and_report(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "FEATHERLESS_API_KEY", "")
    info = client.get("/api/incident").json()
    assert info["reference_time_confirmed"] is False
    assert client.put("/api/incident", json={"reference_time": "2026-10-12T17:42:00"}).status_code == 422  # no offset

    _upload("v1_telematics.csv", (DATA / "sensor" / "v1_telematics.csv").read_bytes())
    client.post("/api/analysis/run")
    md = client.get("/api/report?format=markdown").text
    # No reference time set: t = 0 is the earliest timestamp in the evidence, and the report says so.
    assert "from the evidence: earliest timestamp in v1_telematics.csv" in md and "| 17:42:00 |" in md

    r = client.put("/api/incident", json={"incident_id": "CASE-17", "title": "Main Rd junction collision",
                                          "reference_time": "2026-10-12T17:42:05+05:30"})
    assert r.status_code == 200 and r.json()["reference_time_confirmed"] is True
    client.post("/api/analysis/run")
    timeline = client.get("/api/timeline").json()
    assert timeline["incident_id"] == "CASE-17"
    first = timeline["events"][0]
    assert first["t_start_s"] == -5.0  # the telemetry starts at 17:42:00, five seconds before the new t=0
    md = client.get("/api/report?format=markdown").text
    assert md.startswith("# Incident report — CASE-17 — Main Rd junction collision") and "| 17:42:00 |" in md
    assert (config.STORAGE_DIR / "incident.json").exists()
    assert client.get("/api/audit").json()[-0]["event"] in {"analysis_finished", "incident_updated"}
