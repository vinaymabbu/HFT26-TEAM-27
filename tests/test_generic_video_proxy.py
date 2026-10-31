"""Generic fixes made while testing an unseen dataset (single fixed night camera + whole-frame motion proxy).

Nothing here depends on that clip: a flow series without a per-view prefix, flow statistics other than p95, and
honest timing for an AI keyframe claim.
"""
import json
from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.extraction.sensor import extract_sensor
from app.schemas import Evidence, SourceType

# a calm scene, then sustained motion (mean flow up, p95 only modestly), then calm again
MEAN = [None, 1.7, 3.6, 4.2, 4.4, 4.0, 2.3, 1.5, 1.1, 0.9, 0.8, 0.5, 1.8, 2.5, 1.9, 1.7, 1.2, 1.6, 1.8, 2.7]
P95 = [None, 8.5, 15.0, 14.6, 16.9, 17.8, 11.3, 7.4, 4.3, 1.7, 1.5, 1.6, 9.9, 13.3, 10.2, 9.4, 7.1, 10.7, 9.1, 13.0]
SCOPE = "downscaled whole-frame pixel motion proxy; not physical vehicle telemetry"


def _proxy_csv(path):
    rows = ["timestamp_s,mean_optical_flow_px_per_sample,p95_optical_flow_px_per_sample,measurement_scope"]
    for t, (m, p) in enumerate(zip(MEAN, P95)):
        rows.append(f"{t}.0,{'' if m is None else m},{'' if p is None else p},{SCOPE}")
    path.write_text("\n".join(rows) + "\n")
    return path


def _evidence(path):
    return Evidence(id="ev_p", kind=SourceType.sensor, filename=path.name, size_bytes=1, stored_path=str(path),
                    uploaded_at=datetime.now(timezone.utc))


def test_whole_frame_flow_series_is_read_and_labelled(tmp_path):
    st = extract_sensor(_evidence(_proxy_csv(tmp_path / "proxy.csv")))
    spikes = [o for o in st.observations if o.action == "motion_spike"]
    assert spikes, st.warnings
    assert all(o.attributes["view"] == "whole frame" and o.vehicle_id is None for o in spikes)
    assert all(o.attributes["flow_unit"] == "px per sample" for o in spikes)  # not mislabelled "per frame"
    assert any("not physical vehicle telemetry" in w for w in st.warnings)  # the file's own caveat is kept


def test_a_spike_in_the_mean_flow_is_found_even_when_p95_is_unremarkable(tmp_path):
    st = extract_sensor(_evidence(_proxy_csv(tmp_path / "proxy.csv")))
    spikes = [o for o in st.observations if o.action == "motion_spike"]
    assert any(o.attributes["flow_stat"] == "mean" and 3 <= o.t_rel_s <= 5 for o in spikes)


def test_p95_still_wins_when_it_spikes(tmp_path):
    p95 = list(P95)
    p95[10] = 60.0  # a clear p95 spike where the mean is calm
    rows = ["timestamp_s,mean_optical_flow_px_per_sample,p95_optical_flow_px_per_sample"]
    rows += [f"{t}.0,{'' if m is None else m},{'' if p is None else p}" for t, (m, p) in enumerate(zip(MEAN, p95))]
    f = tmp_path / "p.csv"
    f.write_text("\n".join(rows) + "\n")
    spikes = [o for o in extract_sensor(_evidence(f)).observations if o.action == "motion_spike"]
    assert any(o.t_rel_s == 10.0 and o.attributes["flow_stat"] == "p95" for o in spikes)


def test_upload_accepts_the_series_without_a_speed_warning(tmp_path):
    client = TestClient(app)
    r = client.post("/api/evidence/sensor", files={"file": ("proxy.csv", _proxy_csv(tmp_path / "proxy.csv").read_bytes())})
    assert r.status_code == 201, r.text
    assert r.json()["details"]["upload_warnings"] == []  # a flow series is not expected to carry speed


def _fixed_analysis(collision_at: int, n: int = 6, spacing: float = 0.8):
    from app.modules.ingestion import frames_to_observations
    from app.modules.ingestion.video import Frame, VideoInfo
    from app.modules.ingestion.vision import parse_frame_analysis

    img = np.zeros((9, 16, 3), np.uint8)
    frames = [Frame(index=round(i * spacing * 30), t_s=round(i * spacing, 3), image=img) for i in range(n)]
    replies = [{"vehicles": [], "collision": {"visible": i >= collision_at, "confidence": 0.8, "vehicles": ["white sedan", "white car"]}}
               for i in range(n)]
    ev = Evidence(id="ev_v", kind=SourceType.video, filename="v.mp4", size_bytes=1, stored_path="/dev/null",
                  uploaded_at=datetime.now(timezone.utc))
    info = VideoInfo(fps=30, frame_count=600, width=1920, height=1080)
    analysed = [(f, parse_frame_analysis(json.dumps(r))) for f, r in zip(frames, replies)]
    return frames_to_observations(ev, info, analysed, mode="fixed")


def test_a_collision_seen_in_a_keyframe_carries_the_keyframe_gap_as_uncertainty():
    (c,) = [o for o in _fixed_analysis(collision_at=3) if o.action == "collision"]
    assert c.t_rel_s == pytest.approx(2.4)
    # it happened at or before 2.4 s, somewhere since the previous keyframe 0.8 s earlier; 0.125 s would overstate it
    assert c.time_uncertainty_s == pytest.approx(0.8)
    assert "at or before" in c.attributes["timing"]


def test_state_changes_use_half_the_actual_gap():
    from app.modules.ingestion import frames_to_observations
    from app.modules.ingestion.video import Frame, VideoInfo
    from app.modules.ingestion.vision import parse_frame_analysis

    img = np.zeros((9, 16, 3), np.uint8)
    frames = [Frame(index=i * 60, t_s=i * 2.0, image=img) for i in range(3)]  # keyframes 2 s apart
    replies = [{"signal_heads_visible": True, "signals": [{"approach": "northbound", "state": s, "confidence": 0.8}]}
               for s in ("green", "green", "red")]
    ev = Evidence(id="ev_v", kind=SourceType.video, filename="v.mp4", size_bytes=1, stored_path="/dev/null",
                  uploaded_at=datetime.now(timezone.utc))
    obs = frames_to_observations(ev, VideoInfo(fps=30, frame_count=180, width=1920, height=1080),
                                 [(f, parse_frame_analysis(json.dumps(r))) for f, r in zip(frames, replies)], mode="fixed")
    red = next(o for o in obs if o.attributes.get("signal_state") == "red")
    assert red.time_uncertainty_s == pytest.approx(1.0)  # the change happened somewhere in the last 2 s


# ----------------------------------------------- collision evidence in vehicle descriptions, box tracks for fixed cameras

def _observations(replies, spacing=0.8, mode="fixed"):
    from app.modules.ingestion import frames_to_observations
    from app.modules.ingestion.video import Frame, VideoInfo
    from app.modules.ingestion.vision import parse_frame_analysis

    img = np.zeros((9, 16, 3), np.uint8)
    frames = [Frame(index=round(i * spacing * 30), t_s=round(i * spacing, 3), image=img) for i in range(len(replies))]
    ev = Evidence(id="ev_v", kind=SourceType.video, filename="v.mp4", size_bytes=1, stored_path="/dev/null",
                  uploaded_at=datetime.now(timezone.utc))
    info = VideoInfo(fps=30, frame_count=600, width=1920, height=1080)
    analysed = [(f, parse_frame_analysis(json.dumps(r))) for f, r in zip(frames, replies)]
    return ev, info, analysed, frames_to_observations(ev, info, analysed, mode)


def _veh(label, desc, box=(100, 100, 200, 200), conf=0.8):
    return {"label": label, "box": list(box), "description": desc, "confidence": conf}


def test_a_vehicle_described_as_collided_dates_the_collision_before_the_collision_field():
    replies = [{"vehicles": [_veh("white sedan", "driving in the left lane")]},
               {"vehicles": [_veh("white sedan", "white sedan collided with another car")]},   # 0.8 s
               {"vehicles": [_veh("white sedan", "stopped")], "collision": {"visible": True, "confidence": 0.5, "vehicles": ["a", "b"]}}]
    _, _, _, obs = _observations(replies)
    (c,) = [o for o in obs if o.action == "collision"]  # one collision, from the earlier evidence
    assert c.t_rel_s == pytest.approx(0.8) and c.confidence == pytest.approx(0.56)  # 0.7 x the sighting's 0.8
    assert "vehicle description" in c.attributes["reported_in"] and c.time_uncertainty_s == pytest.approx(0.8)


def test_a_crash_barrier_is_not_a_collision():
    replies = [{"vehicles": [_veh("white car", "white car driving beside the crash barrier")]}] * 3
    assert not [o for o in _observations(replies)[3] if o.action == "collision"]


def _moving(label, x0, n=4):
    return [{"vehicles": [_veh(label, "moving", (x0 + 80 * i, 400, x0 + 80 * i + 100, 500))]} for i in range(n)]


def test_fixed_camera_box_tracks_use_the_bottom_centre_and_are_marked_inferred():
    from app.modules.ingestion import box_tracks

    replies = [{"vehicles": [_veh("white van", "moving", (100 + 80 * i, 400, 200 + 80 * i, 500))]} for i in range(4)]
    ev, info, analysed, _ = _observations(replies)
    (t,) = box_tracks(ev, info, analysed, fixed=True)
    assert t.attributes["camera"] == "fixed" and t.attributes["track_id"] == "B1-001" and t.basis == "inferred"
    assert t.attributes["points"][0][1:] == pytest.approx([150 / 1000 * 1920, 500 / 1000 * 1080])  # bottom centre of the box
    assert t.confidence < 0.5  # weaker than a motion track
    boxes = t.attributes["boxes"]
    assert len(boxes) == len(t.attributes["points"]) == 4  # one detection box per point, in pixels
    assert boxes[0] == pytest.approx([100 / 1000 * 1920, 400 / 1000 * 1080, 200 / 1000 * 1920, 500 / 1000 * 1080])


def test_a_box_track_that_duplicates_a_motion_track_is_dropped():
    from app.modules.ingestion import box_tracks
    from app.schemas import Observation, SourceRef

    replies = [{"vehicles": [_veh("white van", "moving", (100 + 80 * i, 400, 200 + 80 * i, 500))]} for i in range(4)]
    ev, info, analysed, _ = _observations(replies)
    same = [[round(i * 0.8, 3), (150 + 80 * i) / 1000 * 1920 + 10, 500 / 1000 * 1080 - 5] for i in range(4)]
    motion = Observation(id="m1", source_type="video", source_ref=SourceRef(evidence_id="ev_v", file="v.mp4", locator="frames:0-9"),
                         action="track", confidence=0.6, attributes={"points": same})
    assert box_tracks(ev, info, analysed, fixed=True, existing=[motion]) == []
    far = [[t, x + 900, y] for t, x, y in same]
    motion.attributes["points"] = far
    assert len(box_tracks(ev, info, analysed, fixed=True, existing=[motion])) == 1


def test_collision_and_box_tracks_see_sightings_the_fixed_camera_filter_drops():
    """In a crash the model labels vehicles 'stopped' / 'braking'; the fixed-camera filter removes those as events,
    but a vehicle described as collided is still evidence, so it is searched in the unfiltered sightings."""
    from app.modules.ingestion import aggregate_fixed, frames_to_observations

    replies = [{"vehicles": [_veh("white sedan", "moving normally")]},
               {"vehicles": [dict(_veh("white sedan", "white sedan collided with another car"), action="stopped")]}]
    ev, info, analysed, _ = _observations(replies)
    filtered, persistence, dropped = aggregate_fixed(analysed, None)
    assert dropped["stopped"] == 1 and not filtered[1][1].vehicles  # the filter removed it
    obs = frames_to_observations(ev, info, filtered, "fixed", persistence, raw=analysed)
    assert [o.t_rel_s for o in obs if o.action == "collision"] == [pytest.approx(0.8)]


def _single_source_collision(camera: str):
    from app.modules.contradictions import detect_conflicts
    from app.modules.timeline import build_timeline
    from app.schemas import Observation, ObservationSet, SourceRef

    obs = Observation(id="v1", source_type="video", source_ref=SourceRef(evidence_id="ev_v", file="v.mp4", locator="frame:100"),
                      t_rel_s=4.8, time_uncertainty_s=0.5, action="collision", confidence=0.6,
                      attributes={"vehicles": ["white sedan", "white car"], "camera": camera})
    sets = [ObservationSet(source_type="video", evidence_id="ev_v", producer="t", generated_at=datetime.now(timezone.utc), observations=[obs])]
    report = detect_conflicts(sets, build_timeline(sets))
    return next(c for c in report.conflicts if c.type.value == "missing_evidence").description


def test_single_source_wording_follows_the_camera_type():
    assert "supported only by the camera footage" in _single_source_collision("fixed")
    assert "supported only by the dashcam" in _single_source_collision("dashcam")
