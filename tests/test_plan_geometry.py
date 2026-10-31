"""Road-aligned schematic plan: vanishing point from the picture, ground mapping, and its use in the reconstruction.

Truth comes from scripts/synthetic.py::camera_pose, a camera that is pitched down and yawed away from the road.
"""
import itertools
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from synthetic import camera_pose, render_cctv_pose  # noqa: E402

from app.modules.ingestion.road_geometry import estimate_vanishing_point, traffic_agrees  # noqa: E402
from app.modules.reconstruction.plan import road_plan  # noqa: E402

W, H, FOCAL = 640, 360, 560.0


def _empty_road(tmp_path, pitch, yaw, cam_height=8.0):
    path = render_cctv_pose(tmp_path / "road.avi", [], 0.5, w=W, h=H, focal=FOCAL, cam_height=cam_height,
                            pitch_deg=pitch, yaw_deg=yaw)
    ok, img = cv2.VideoCapture(str(path)).read()
    assert ok
    return img


@pytest.mark.parametrize("pitch,yaw", [(22, -20), (26, 12), (20, -25)])
def test_vanishing_point_matches_the_camera_pose(tmp_path, pitch, yaw):
    vp, why = estimate_vanishing_point(_empty_road(tmp_path, pitch, yaw))
    _, truth = camera_pose(W, H, FOCAL, 8.0, pitch, yaw)
    assert vp is not None, why
    assert math.hypot(vp.x - truth[0], vp.y - truth[1]) < 3.0
    assert vp.distinct_lines >= 3 and vp.inliers >= 5


def test_a_picture_without_lines_has_no_vanishing_point():
    flat = np.full((360, 640, 3), 90, np.uint8)
    noise = np.random.default_rng(1).integers(0, 255, (360, 640, 3), dtype=np.uint8)
    for img in (flat, noise):
        vp, why = estimate_vanishing_point(img)
        assert vp is None and why


def test_a_single_line_is_not_enough_to_trust(tmp_path):
    img = np.full((360, 640, 3), 90, np.uint8)
    cv2.line(img, (100, 359), (400, 40), (240, 240, 240), 4)
    vp, why = estimate_vanishing_point(img)
    assert vp is None


def _grid_error(plan, project, points):
    uv = [project(X, 0, Z) for X, Z in points]
    plan_pts = [plan(*p) for p in uv]
    dt = np.array([math.dist(a, b) for a, b in itertools.combinations(points, 2)])
    dp = np.array([math.dist(a, b) for a, b in itertools.combinations(plan_pts, 2)])
    s = float(dt @ dp) / float(dp @ dp)
    return float(np.sqrt(np.mean(((s * dp - dt) / dt) ** 2)))


def test_ground_mapping_is_exact_up_to_scale_when_the_field_of_view_is_right():
    project, vp = camera_pose(W, H, FOCAL, 7.0, 22, -20)
    pts = [(X, Z) for X in (-5.6, -1.9, 1.9, 5.6) for Z in (12, 20, 32, 48)]
    pts = [p for p in pts if (uv := project(p[0], 0, p[1])) and 0 <= uv[0] < W and 0 <= uv[1] < H]
    assert len(pts) >= 6
    assert _grid_error(road_plan(vp, W, H, focal_per_width=FOCAL / W), project, pts) < 0.005


def test_ground_mapping_degrades_gently_when_the_field_of_view_is_wrong():
    # The assumed 0.87 x width focal length against a camera that is really 0.7 x or 1.2 x: shape error stays small.
    for true_f in (0.7 * W, 1.2 * W):
        project, vp = camera_pose(W, H, true_f, 7.0, 22, -20)
        pts = [(X, Z) for X in (-5.6, -1.9, 1.9, 5.6) for Z in (12, 20, 32, 48)]
        pts = [p for p in pts if (uv := project(p[0], 0, p[1])) and 0 <= uv[0] < W and 0 <= uv[1] < H]
        assert len(pts) >= 6
        assert _grid_error(road_plan(vp, W, H), project, pts) < 0.12


def test_far_field_points_stay_finite():
    ground = road_plan((540.0, -46.0), W, H)
    for v in (-46.0, -45.9, -40.0, 0.0):  # at and above the horizon: clamped, never infinite
        x, y = ground(300.0, v)
        assert math.isfinite(x) and math.isfinite(y)
    assert ground(300.0, 300.0)[1] < ground(300.0, 100.0)[1]  # lower in the image = nearer = smaller along-road distance


def test_traffic_check_only_vetoes_with_enough_long_tracks():
    vp = (300.0, -50.0)
    along = [(0.0, 300.0, 300.0), (1.0, 300.0, 200.0), (2.0, 300.0, 100.0)]    # straight at the vanishing point
    across = [(0.0, 100.0, 200.0), (1.0, 200.0, 200.0), (2.0, 300.0, 200.0)]   # sideways
    assert traffic_agrees(vp, [across], 300)[0]                    # one track proves nothing
    assert traffic_agrees(vp, [across, across], 300)[0]            # nor do two
    assert not traffic_agrees(vp, [across, across, across, along], 300)[0]
    assert traffic_agrees(vp, [along, along, along, across], 300)[0]


def _observation(points, vp=None, camera="fixed"):
    from app.schemas import Observation, SourceRef
    attrs = {"track_id": "T1-001", "view": 1, "camera": camera, "units": "px", "view_box": [0, 0, W, H],
             "points": points, "direction": "away from camera"}
    if vp:
        attrs["vanishing_point_px"] = list(vp)
    return Observation(id="vid_t1", source_type="video", source_ref=SourceRef(evidence_id="ev_v", file="v.mp4", locator="frames:1-9"),
                       t_rel_s=points[0][0], time_uncertainty_s=0.1, action="track", confidence=0.6, attributes=attrs)


def _plan(points, vp):
    from app.modules.reconstruction.camera import reconstruct_schematic_from_camera
    from app.modules.timeline import build_timeline
    from app.schemas import ObservationSet
    sets = [ObservationSet(source_type="video", evidence_id="ev_v", producer="t", generated_at=datetime.now(timezone.utc),
                           observations=[_observation(points, vp)])]
    return reconstruct_schematic_from_camera(sets, build_timeline(sets))


def test_a_vehicle_driving_down_its_lane_runs_straight_up_the_road_aligned_plan():
    project, vp = camera_pose(W, H, FOCAL, 8.0, 22, -20)
    pts = [[2.0 + 0.2 * k, *project(-1.875, 0.0, 14.0 + 1.6 * k)] for k in range(14)]
    aligned = _plan(pts, vp)
    nominal = _plan(pts, None)
    def angle(rec):
        t = rec.vehicles[0].track
        return math.degrees(math.atan2(abs(t[-1].x_m - t[0].x_m), abs(t[-1].y_m - t[0].y_m)))
    assert angle(aligned) < 2.0                 # straight along the road
    assert angle(nominal) > 10.0                # the nominal camera leaves the road diagonal
    assert "direction comes from the lane lines" in aligned.note and "direction" not in nominal.note.split("NOT to scale")[1][:60]
    # the plan's labelling guarantees are unchanged
    for rec in (aligned, nominal):
        assert rec.scene.units == "schematic" and rec.collision is None and "NOT to scale" in rec.note and "calibrat" in rec.note
        assert all(p.source == "estimated" and p.speed_kmh is None and p.confidence <= 0.3 for p in rec.vehicles[0].track)
        depths = [p.y_m for p in rec.vehicles[0].track]
        assert depths == sorted(depths)         # the track walks away from the camera: depth grows


def test_schematic_confidence_follows_the_evidence_inside_a_low_ceiling():
    project, vp = camera_pose(W, H, FOCAL, 8.0, 22, -20)
    pts = [[2.0 + 0.2 * k, *project(-1.875, 0.0, 14.0 + 1.6 * k)] for k in range(14)]
    aligned, nominal = _plan(pts, vp).vehicles[0].track, _plan(pts, None).vehicles[0].track
    confs = [p.confidence for p in aligned]
    assert len(set(confs)) > 1                                       # not one flat number for every point
    assert 0.10 <= min(confs) and max(confs) <= 0.30                 # an uncalibrated plan stays low
    by_row = sorted(zip((r[2] for r in pts), confs))
    assert by_row[0][1] < by_row[-1][1]                              # farther from the camera (higher in the view) = less sure
    assert all(n.confidence < a.confidence for n, a in zip(nominal, aligned))   # an assumed road direction is worth less than a found one


def test_the_pipeline_attaches_the_road_direction_to_its_tracks(tmp_path, monkeypatch):
    from app import config
    from app.modules import ingestion
    from app.modules.ingestion import analyze_video
    from app.schemas import Evidence, SourceType

    monkeypatch.setattr(config, "FEATHERLESS_API_KEY", "")
    monkeypatch.setattr(ingestion, "CAMERA_MODE", "fixed")
    veh = [{"lane_x": -1.875, "z0": 22.0, "speed_ms": 9.0, "color": (40, 40, 200)},
           {"lane_x": 1.875, "z0": 90.0, "speed_ms": -12.0, "color": (200, 120, 40)}]
    path = render_cctv_pose(tmp_path / "p.avi", veh, 12.0, w=W, h=H, focal=FOCAL, cam_height=9.0, pitch_deg=22.0, yaw_deg=-20.0)
    ev = Evidence(id="ev_p", kind=SourceType.video, filename="p.avi", size_bytes=1, stored_path=str(path),
                  uploaded_at=datetime.now(timezone.utc))
    obs = analyze_video(ev)
    tracks = [o for o in obs.observations if o.action == "track"]
    assert tracks
    _, truth = camera_pose(W, H, FOCAL, 9.0, 22.0, -20.0)
    vp = tracks[0].attributes["vanishing_point_px"]
    assert math.hypot(vp[0] - truth[0], vp[1] - truth[1]) < 4.0
    assert tracks[0].attributes["road_geometry"]["distinct_lines"] >= 3
    assert any("road direction found" in w for w in obs.warnings)


def test_a_failure_in_the_road_or_linking_enhancements_never_aborts_the_video_stage(tmp_path, monkeypatch):
    from app import config
    from app.modules import ingestion
    from app.modules.ingestion import analyze_video
    from app.schemas import Evidence, SourceType

    def boom(*a, **k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(config, "FEATHERLESS_API_KEY", "")
    monkeypatch.setattr(ingestion, "CAMERA_MODE", "fixed")
    monkeypatch.setattr(ingestion, "estimate_vanishing_point", boom)
    veh = [{"lane_x": -1.875, "z0": 22.0, "speed_ms": 9.0, "color": (40, 40, 200)},
           {"lane_x": 1.875, "z0": 90.0, "speed_ms": -12.0, "color": (200, 120, 40)}]
    path = render_cctv_pose(tmp_path / "p.avi", veh, 12.0, w=W, h=H, focal=FOCAL, cam_height=9.0, pitch_deg=22.0, yaw_deg=-20.0)
    ev = Evidence(id="ev_p", kind=SourceType.video, filename="p.avi", size_bytes=1, stored_path=str(path),
                  uploaded_at=datetime.now(timezone.utc))
    obs = analyze_video(ev)
    assert [o for o in obs.observations if o.action == "track"]                      # the motion tracks still stand
    assert any("could not be estimated" in w for w in obs.warnings)
    assert not any("vanishing_point_px" in o.attributes for o in obs.observations)   # and no road direction is claimed


def test_tiny_or_degenerate_views_do_not_raise():
    for shape in ((10, 10, 3), (63, 400, 3), (400, 63, 3)):
        vp, why = estimate_vanishing_point(np.zeros(shape, np.uint8))
        assert vp is None and why


def test_ai_sighted_tracks_are_named_and_coloured_each_and_motion_tracks_keep_their_view_colour():
    from app.modules.reconstruction.camera import AI_COLORS, VIEW_COLORS, reconstruct_schematic_from_camera
    from app.modules.timeline import build_timeline
    from app.schemas import ObservationSet

    def track(i, label=None, x0=100):
        o = _observation([[2.0 + 0.5 * k, x0 + 20 * k, 100.0 + 25 * k] for k in range(6)])
        o = o.model_copy(update={"id": f"vid_t{i}", "attributes": {**o.attributes, "track_id": f"{'B' if label else 'T'}1-00{i}",
                                                                      **({"label": label, "source": "AI vehicle boxes"} if label else {})}})
        return o
    sets = [ObservationSet(source_type="video", evidence_id="ev_v", producer="t", generated_at=datetime.now(timezone.utc),
                           observations=[track(1, "white van"), track(2, "dark SUV", 300), track(3)])]
    rec = reconstruct_schematic_from_camera(sets, build_timeline(sets))
    by = {v.vehicle_id: v for v in rec.vehicles}
    assert by["B1-001"].label == "B1-001 · white van" and by["B1-002"].label == "B1-002 · dark SUV"
    assert by["B1-001"].color != by["B1-002"].color and {by["B1-001"].color, by["B1-002"].color} <= set(AI_COLORS)
    assert by["T1-003"].label == "T1-003" and by["T1-003"].color == VIEW_COLORS[0]
