"""Fixed-camera trajectories: grid detection, motion tracking and the camera-image reconstruction."""
from datetime import datetime, timezone

import cv2
import numpy as np

from app import config
from app.modules import ingestion
from app.modules.ingestion import analyze_video
from app.modules.ingestion.video import detect_grid, probe_video, track_vehicles
from app.modules.reconstruction import build_event_analysis, build_reconstruction
from app.modules.timeline import build_timeline
from app.schemas import ConflictReport, Evidence, SourceType

W, H, FPS, N = 320, 200, 10, 70


def _cctv(path):
    """2x2 grid of static 'roads' with one car moving down view 1 and one across view 4."""
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), FPS, (W, H))
    rng = np.random.default_rng(0)
    road = cv2.GaussianBlur(rng.integers(60, 140, (H, W), dtype=np.uint8), (9, 9), 0)
    for i in range(N):
        img = cv2.cvtColor(road, cv2.COLOR_GRAY2BGR)
        img[:, W // 2 - 1:W // 2 + 1] = 0  # view separators
        img[H // 2 - 1:H // 2 + 1, :] = 0
        y = 30 + i                          # view 1: moves down 1 px/frame
        cv2.rectangle(img, (60, y), (76, y + 12), (240, 240, 240), -1)
        x = W // 2 + 10 + 2 * i % 130       # view 4: moves right 2 px/frame
        cv2.rectangle(img, (x, 160), (x + 14, 172), (30, 30, 220), -1)
        w.write(img)
    w.release()
    return path


def _ev(path):
    return Evidence(id="ev_cctv", kind=SourceType.video, filename=path.name, size_bytes=path.stat().st_size,
                    stored_path=str(path), uploaded_at=datetime.now(timezone.utc))


def test_grid_detection():
    img = np.full((100, 200, 3), 120, np.uint8)
    assert detect_grid(img) == [(0, 0, 200, 100)]
    img[:, 99:101] = 0
    img[49:51, :] = 0
    assert detect_grid(img) == [(0, 0, 99, 49), (101, 0, 200, 49), (0, 51, 99, 100), (101, 51, 200, 100)]


def test_tracker_follows_moving_cars(tmp_path):
    path = _cctv(tmp_path / "cctv.avi")
    info = probe_video(str(path))
    tracks = track_vehicles(str(path), info, detect_grid(cv2.VideoCapture(str(path)).read()[1]), warmup_s=1.0)
    by_view = {t.view: t for t in tracks}
    assert set(by_view) >= {1, 4}
    down = by_view[1].points
    assert down[-1][2] - down[0][2] >= 25 and abs(down[-1][1] - down[0][1]) < 6  # straight down (to the view edge)
    across = by_view[4].points
    assert across[-1][1] - across[0][1] > 30  # left to right


def test_tracks_without_key_become_a_camera_reconstruction(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FEATHERLESS_API_KEY", "")
    monkeypatch.setattr(ingestion, "CAMERA_MODE", "fixed")
    path = _cctv(tmp_path / "cctv.avi")
    obs = analyze_video(_ev(path))  # no key: tracks only, not "skipped"
    tracks = [o for o in obs.observations if o.action == "track"]
    assert tracks and all(o.attributes["units"] == "px" and len(o.attributes["points"]) >= 2 for o in tracks)
    assert any("no vision-model findings" in w for w in obs.warnings)

    timeline = build_timeline([obs])
    assert timeline.events == []  # raw tracks are not timeline events
    empty = ConflictReport(incident_id="x", generated_at=datetime.now(timezone.utc), conflicts=[])
    # No view is calibrated: no metric map, but a labelled schematic estimate (no speeds, no impact point).
    top_down = build_reconstruction([obs], timeline, empty)
    assert top_down.scene.units == "schematic" and top_down.vehicles and top_down.collision is None
    assert "NOT to scale" in top_down.note and "calibrat" in top_down.note
    assert all(p.source == "estimated" and p.speed_kmh is None for v in top_down.vehicles for p in v.track)
    # The footage with its tracks is event analysis (pixels).
    recon = build_event_analysis([obs], timeline)
    assert recon.scene.type == "camera_image" and recon.scene.units == "px"
    assert recon.scene.image.evidence_id == "ev_cctv" and recon.scene.image.fps == FPS
    assert len(recon.scene.image.views) == 4
    assert {v.vehicle_id[:2] for v in recon.vehicles} >= {"T1", "T4"}
    first = recon.vehicles[0].track
    assert first == sorted(first, key=lambda p: p.t_s)
    # Every footage point carries the detection box around the vehicle: it contains the road-contact point.
    for v in recon.vehicles:
        for p in v.track:
            if p.box is not None:
                x1, y1, x2, y2 = p.box
                assert x1 < x2 and y1 < y2 and x1 - 1 <= p.x_m <= x2 + 1 and y1 - 1 <= p.y_m <= y2 + 1
    assert any(p.box for v in recon.vehicles if v.vehicle_id[0] == "T" for p in v.track)
    # In the footage (pixels) the motion tracker's points are observed in the image itself: nothing is assumed.
    moving = [v for v in recon.vehicles if v.vehicle_id[0] == "T"]
    assert moving and all(p.source == "tracked" for v in moving for p in v.track)
    assert all(v.assumptions == [] and v.source_observation_ids for v in moving)


def _px_track(camera: str, view_box=(0, 0, 480, 300)):
    from app.schemas import Observation, SourceRef, SourceType
    pts = [[2.0 + 0.2 * k, 240.0, 100.0 + 20 * k] for k in range(8)]  # walking down the image, toward the camera
    return Observation(id="vid_t1", source_type=SourceType.video, source_ref=SourceRef(evidence_id="ev_v", file="v.mp4", locator="frames:1-9"),
                       t_rel_s=2.0, time_uncertainty_s=0.1, action="track", confidence=0.6,
                       attributes={"track_id": "T1-001", "view": 1, "camera": camera, "units": "px", "view_box": list(view_box),
                                   "points": pts, "direction": "towards camera"})


from app.schemas import ObservationSet as _OS  # noqa: E402


def test_schematic_plan_is_labelled_and_makes_no_metric_claims():
    from app.modules.reconstruction.camera import reconstruct_schematic_from_camera
    sets = [_OS(source_type="video", evidence_id="ev_v", producer="t", generated_at=datetime.now(timezone.utc),
                           observations=[_px_track("fixed")])]
    tl = build_timeline(sets)
    rec = reconstruct_schematic_from_camera(sets, tl)
    assert rec.scene.units == "schematic" and rec.collision is None and "NOT to scale" in rec.note
    track = rec.vehicles[0].track
    assert all(p.source == "estimated" and p.speed_kmh is None and p.confidence <= 0.3 for p in track)
    depths = [p.y_m for p in track]
    assert depths == sorted(depths, reverse=True)  # lower in the image = closer to the camera = smaller depth
    assert rec.t_min_s == 2.0


def test_no_schematic_plan_for_a_moving_camera():
    from app.modules.reconstruction.camera import reconstruct_schematic_from_camera
    sets = [_OS(source_type="video", evidence_id="ev_v", producer="t", generated_at=datetime.now(timezone.utc),
                           observations=[_px_track("moving")])]
    assert reconstruct_schematic_from_camera(sets, build_timeline(sets)) is None
