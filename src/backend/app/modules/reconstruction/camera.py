"""Reconstruction on the camera image, for fixed-camera video without metric positions.

Uses the motion tracks from module 1 (action "track", pixel coordinates) and places vision-verified
stopped vehicles at their box centres. Coordinates stay in image pixels: turning them into metres
would need a camera calibration (a homography per view), which the evidence does not provide.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone

from .plan import road_plan
from app.schemas import CameraImage, Observation, ObservationSet, Reconstruction, Scene, Timeline, TrackPoint, VehicleTrack

VIEW_COLORS = ["#0ea5e9", "#22c55e", "#f59e0b", "#a855f7", "#14b8a6", "#ec4899"]
STOPPED_COLOR = "#dc2626"
# AI-sighted vehicles are few and each has an identity worth telling apart: one colour each (dense motion-tracked
# scenes keep one colour per camera view instead).
AI_COLORS = ["#0ea5e9", "#f97316", "#22c55e", "#a855f7", "#eab308", "#ec4899", "#14b8a6", "#ef4444", "#6366f1", "#84cc16"]


def _is_ai(o: Observation) -> bool:
    return bool(o.attributes.get("source"))


def _name(o: Observation, view) -> str:
    """Track id, plus what the model called the vehicle for an AI-sighted track, else the camera view."""
    tid = o.attributes["track_id"]
    return f"{tid} · {o.attributes['label']}" if _is_ai(o) and o.attributes.get("label") else f"{tid} · view {view}"


def _heading(dx: float, dy: float) -> float:
    """Direction of motion on the image, degrees clockwise from image-up."""
    return round(math.degrees(math.atan2(dx, -dy)) % 360, 1)


def _offset(tracks: list[Observation]) -> float:
    """t_rel_s - frame / fps for the tracks (the camera clock offset), so a time maps back to a frame."""
    for o in tracks:
        fps = o.attributes.get("fps")
        loc = o.source_ref.locator
        if fps and loc.startswith("frames:"):
            first = int(loc.split(":")[1].split("-")[0])
            return round(o.attributes["points"][0][0] - first / float(fps), 3)
    return 0.0


def reconstruct_from_camera(observation_sets: list[ObservationSet], timeline: Timeline) -> Reconstruction | None:
    observations = [o for s in observation_sets for o in s.observations]
    tracks = [o for o in observations if o.action == "track" and o.attributes.get("units") == "px"]
    stopped = [o for o in observations if o.action == "stopped" and o.attributes.get("box")
               and o.attributes.get("camera") == "fixed"]
    if not tracks and not stopped:
        return None
    # One video per scene: the one with the most tracks.
    evidence_id = Counter(o.source_ref.evidence_id for o in tracks + stopped).most_common(1)[0][0]
    tracks = [o for o in tracks if o.source_ref.evidence_id == evidence_id]
    stopped = [o for o in stopped if o.source_ref.evidence_id == evidence_id]
    ref = (tracks or stopped)[0].attributes
    width, height = ref.get("frame_size", [0, 0])
    fps = float(next((o.attributes["fps"] for o in tracks if o.attributes.get("fps")), 0) or 0)
    views = next((o.attributes["grid"] for o in tracks if o.attributes.get("grid")), None) or sorted(
        {tuple(o.attributes["view_box"]) for o in tracks if o.attributes.get("view_box")}, key=lambda b: (b[1], b[0]))

    vehicles: list[VehicleTrack] = []
    n_ai = 0
    for o in tracks:
        pts = o.attributes["points"]
        speeds = {round(t, 3): s for t, s in o.attributes.get("speed_points", [])}
        track = []
        for k, (t, x, y) in enumerate(pts):
            a, b = pts[max(0, k - 1)], pts[min(len(pts) - 1, k + 1)]
            track.append(TrackPoint(t_s=t, x_m=x, y_m=y, heading_deg=_heading(b[1] - a[1], b[2] - a[2]),
                                    speed_kmh=speeds.get(round(t, 3)), seen_in_video=True))
        view = o.attributes["view"]
        mean = o.attributes.get("mean_speed_kmh")
        speed = f"~{mean:g} km/h ({o.attributes.get('calibration', '').replace('_', ' ')} calibration)" if mean else \
            f"~{o.attributes.get('mean_speed_px_s', '?')} px/s (view not calibrated)"
        if _is_ai(o):
            n_ai += 1
        vehicles.append(VehicleTrack(
            vehicle_id=o.attributes["track_id"], label=_name(o, view),
            color=AI_COLORS[(n_ai - 1) % len(AI_COLORS)] if _is_ai(o) else VIEW_COLORS[(view - 1) % len(VIEW_COLORS)],
            description=(f"AI-sighted vehicle, {o.attributes.get('direction', '')}; coarse, from the model's boxes" if o.attributes.get("source")
                         else f"moving, {o.attributes.get('direction', '')}, {speed}"),
            track=track, source_observation_ids=[o.id]))

    for n, o in enumerate(stopped, start=1):
        x1, y1, x2, y2 = o.attributes["box"]
        w, h = o.attributes.get("frame_size", [width, height])
        cx, cy = round((x1 + x2) / 2000 * w, 1), round((y1 + y2) / 2000 * h, 1)
        t0 = o.attributes.get("first_seen_s", o.t_rel_s or 0.0)
        t1 = max(o.attributes.get("last_seen_s", t0), t0)
        view = o.attributes.get("view")
        vehicles.append(VehicleTrack(
            vehicle_id=f"S{view or 0}-{n}", label=f"{o.attributes.get('label', 'vehicle')} (stopped, view {view})",
            color=STOPPED_COLOR, description=o.vehicle_description,
            track=[TrackPoint(t_s=t0, x_m=cx, y_m=cy, heading_deg=0), TrackPoint(t_s=t1, x_m=cx, y_m=cy, heading_deg=0)],
            source_observation_ids=[o.id]))

    times = [p.t_s for v in vehicles for p in v.track]
    t_min, t_max = min(times), max(times)
    background = round(((t_min + t_max) / 2) * fps) if fps else 0
    if stopped:  # show the frame where the verified stop was first confirmed
        background = int(stopped[0].source_ref.locator.split(":")[1]) if stopped[0].source_ref.locator.startswith("frame:") else background
    return Reconstruction(
        incident_id=timeline.incident_id, generated_at=datetime.now(timezone.utc),
        scene=Scene(type="camera_image", units="px",
                    image=CameraImage(evidence_id=evidence_id, frame=background, width=int(width), height=int(height),
                                      views=[list(v) for v in views], fps=fps or None, t_offset_s=_offset(tracks))),
        t_min_s=round(t_min, 3), t_max_s=round(t_max, 3), vehicles=vehicles, collision=None)


def reconstruct_ground_from_camera(observation_sets: list[ObservationSet], timeline: Timeline) -> Reconstruction | None:
    """Top-down reconstruction (metres) from camera tracks of a calibrated view.

    Each calibrated track carries ground_points [[t, X_lateral, Y_along_road]]. Different camera views
    have their own road frames and cannot be overlaid without knowing how the cameras relate, so the
    view with the most calibrated tracks is reconstructed and the others are named in the note.
    """
    tracks = [o for o in (o for s in observation_sets for o in s.observations)
              if o.action == "track" and o.attributes.get("ground_points")]
    if not tracks:
        return None
    per_view: dict[int, list[Observation]] = {}
    for o in tracks:
        per_view.setdefault(o.attributes.get("view", 1), []).append(o)
    view, chosen = max(per_view.items(), key=lambda kv: len(kv[1]))
    lateral_known = all(p[1] is not None for o in chosen for p in o.attributes["ground_points"])
    vehicles = []
    n_ai = 0
    for o in chosen:
        pts = [p for p in o.attributes["ground_points"] if p[2] is not None]
        if len(pts) < 2:
            continue
        speeds = {round(t, 3): s for t, s in o.attributes.get("speed_points", [])}
        ai = _is_ai(o)  # the model's boxes read off the road: inferred positions, no speeds
        n_ai += ai
        track = []
        for k, (t, x, y) in enumerate(pts):
            a, b = pts[max(0, k - 1)], pts[min(len(pts) - 1, k + 1)]
            dx = (b[1] or 0) - (a[1] or 0)
            dy = b[2] - a[2]
            track.append(TrackPoint(t_s=t, x_m=round(x or 0.0, 2), y_m=round(y, 2),
                                    heading_deg=round(math.degrees(math.atan2(dx, dy)) % 360, 1),
                                    speed_kmh=None if ai else speeds.get(round(t, 3)),
                                    source="estimated" if ai else "tracked", confidence=o.confidence if ai else 0.6,
                                    seen_in_video=True))
        mean = o.attributes.get("mean_speed_kmh")
        vehicles.append(VehicleTrack(
            vehicle_id=o.attributes["track_id"], label=_name(o, view) if ai else f"{o.attributes['track_id']}" + (f" · ~{mean:g} km/h" if mean else ""),
            color=AI_COLORS[(n_ai - 1) % len(AI_COLORS)] if ai else VIEW_COLORS[(view - 1) % len(VIEW_COLORS)],
            description=(f"AI-sighted vehicle, {o.attributes.get('direction', '')}; positions on the road from the model's boxes "
                         f"(inferred, no speed), {o.attributes.get('calibration', '').replace('_', ' ')} calibration" if ai else
                         f"{o.attributes.get('direction', '')}; tracked on camera view {view}, "
                         f"{o.attributes.get('calibration', '').replace('_', ' ')} calibration"),
            track=track, source_observation_ids=[o.id]))
    if not vehicles:
        return None
    xs = [p.x_m for v in vehicles for p in v.track]
    ys = [p.y_m for v in vehicles for p in v.track]
    times = [p.t_s for v in vehicles for p in v.track]
    half = max(10.0, (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2) * 1.15
    others = sorted(v for v in per_view if v != view)
    note = (f"Reconstructed from camera view {view} (calibrated): {len(vehicles)} vehicle track(s) on the road plane."
            + ("" if lateral_known else " Lateral positions unknown (lane width not measured), drawn on one line.")
            + (f" {n_ai} of them are AI-sighted (the vision model's boxes seconds apart): inferred positions, no speeds." if n_ai else "")
            + (f" Views {', '.join(map(str, others))} are also calibrated but have their own road frame."
               if others else "") + " Stationary vehicles are not tracked by motion.")
    return Reconstruction(
        incident_id=timeline.incident_id, generated_at=datetime.now(timezone.utc),
        scene=Scene(type="metric", units="m", extent_m=round(half, 1),
                    center_m=(round((max(xs) + min(xs)) / 2, 2), round((max(ys) + min(ys)) / 2, 2)),
                    frame_note=f"camera view {view} road frame: x = across the road (m), "
                               "y = distance from the camera along the road (m)"),
        t_min_s=round(min(times), 3), t_max_s=round(max(times), 3), vehicles=vehicles, collision=None, note=note)


# Nominal pinhole assumptions for the schematic plan view (no calibration exists, so these are stated, not measured).
SCHEMATIC_HORIZON_ABOVE_VIEW = 0.15   # horizon row sits this fraction of the view height above the view's top edge
SCHEMATIC_FOCAL_PER_WIDTH = 0.87      # focal length = 0.87 x view width (about a 60 degree field of view)
SCHEMATIC_NEAR_UNITS = 10.0           # schematic units per view-height of depth: bottom edge of the view = ~8.7 units
MIN_SCHEMATIC_POINTS = 4
# Confidence of a schematic point: an uncalibrated plan never exceeds SCHEMATIC_CONF_MAX, and within that range it
# follows the evidence = the detection's own confidence x how far the plan itself can be trusted x distance from camera.
SCHEMATIC_CONF_MIN, SCHEMATIC_CONF_MAX = 0.10, 0.30
SCHEMATIC_PLAN_FACTOR = {"road_aligned": 1.0, "nominal_camera": 0.6}   # the road direction was found / only assumed
SCHEMATIC_FAR_FACTOR = 0.6            # at the top of the view, where one pixel spans the most road; 1.0 at the bottom edge


def reconstruct_schematic_from_camera(observation_sets: list[ObservationSet], timeline: Timeline) -> Reconstruction | None:
    """A schematic top-down plan from the tracks of a fixed, *uncalibrated* camera view.

    Without lane markings or a manual calibration nothing is measured, so this is a labelled estimate: image
    positions are mapped onto a flat ground plane with a nominal camera (horizon just above the view, ~60 degree
    field of view). It shows who moved where and in what order, with the perspective undone roughly; distances,
    speeds and a collision location are NOT given. Units are "schematic", every point is `estimated`.
    """
    tracks = [o for o in (o for s in observation_sets for o in s.observations)
              if o.action == "track" and o.attributes.get("units") == "px" and o.attributes.get("camera") == "fixed"
              and o.attributes.get("view_box") and len(o.attributes.get("points", [])) >= MIN_SCHEMATIC_POINTS]
    if not tracks:
        return None
    per_view: dict[int, list[Observation]] = {}
    for o in tracks:
        per_view.setdefault(o.attributes.get("view", 1), []).append(o)
    view, chosen = max(per_view.items(), key=lambda kv: len(kv[1]))
    x1, y1, x2, y2 = chosen[0].attributes["view_box"]
    w, h = max(x2 - x1, 1), max(y2 - y1, 1)
    xc, y_h = (x1 + x2) / 2, y1 - SCHEMATIC_HORIZON_ABOVE_VIEW * h
    focal = SCHEMATIC_FOCAL_PER_WIDTH * w
    # Preferred: the road's own direction, found from its lane lines and rails (ingestion/road_geometry.py), so the plan
    # is road-aligned: lanes parallel, the road running up the page. Otherwise a nominal camera that looks along the road.
    vp = next((o.attributes["vanishing_point_px"] for o in chosen if o.attributes.get("vanishing_point_px")), None)
    aligned = road_plan((vp[0] - x1, vp[1] - y1), w, h) if vp else None

    def ground(x: float, y: float) -> tuple[float, float]:
        if aligned is not None:
            return aligned(x - x1, y - y1)
        depth = SCHEMATIC_NEAR_UNITS * h / max(y - y_h, 0.05 * h)
        return (x - xc) * depth / focal, depth

    vehicles = []
    n_ai = 0
    for o in sorted(chosen, key=lambda o: o.attributes["points"][0][0]):
        n_ai += _is_ai(o)
        pts = [(t, *ground(x, y)) for t, x, y in o.attributes["points"]]
        plan_factor = SCHEMATIC_PLAN_FACTOR["road_aligned" if aligned is not None else "nominal_camera"]
        track = []
        for k, (t, gx, gy) in enumerate(pts):
            a, b = pts[max(0, k - 1)], pts[min(len(pts) - 1, k + 1)]
            near = min(max((o.attributes["points"][k][2] - y1) / h, 0.0), 1.0)   # 0 at the top of the view, 1 at the bottom
            quality = min(o.confidence, 1.0) * plan_factor * (SCHEMATIC_FAR_FACTOR + (1 - SCHEMATIC_FAR_FACTOR) * near)
            conf = SCHEMATIC_CONF_MIN + (SCHEMATIC_CONF_MAX - SCHEMATIC_CONF_MIN) * quality
            track.append(TrackPoint(t_s=t, x_m=round(gx, 2), y_m=round(gy, 2),
                                    heading_deg=round(math.degrees(math.atan2(b[1] - a[1], b[2] - a[2])) % 360, 1),
                                    source="estimated", confidence=round(conf, 2), seen_in_video=True))
        vehicles.append(VehicleTrack(
            vehicle_id=o.attributes["track_id"], label=_name(o, view) if _is_ai(o) else o.attributes["track_id"],
            color=AI_COLORS[(n_ai - 1) % len(AI_COLORS)] if _is_ai(o) else VIEW_COLORS[(view - 1) % len(VIEW_COLORS)],
            description=(f"AI-sighted vehicle, {o.attributes.get('direction', '')}; schematic position from camera view {view}" if o.attributes.get("source")
                         else f"moving {o.attributes.get('direction', '')}; schematic position from camera view {view}"),
            track=track, source_observation_ids=[o.id]))
    xs = [p.x_m for v in vehicles for p in v.track]
    ys = [p.y_m for v in vehicles for p in v.track]
    times = [p.t_s for v in vehicles for p in v.track]
    half = max(5.0, (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2) * 1.15
    others = sorted(v for v in per_view if v != view)
    camera = ("Image positions are mapped onto a flat road. The road's direction comes from the lane lines and rails in the "
              "picture (the road runs up the plan); the camera's field of view (about 60 degrees) is assumed, "
              if aligned is not None else
              "Image positions are mapped onto a flat road with an assumed camera (horizon just above the view, about 60 degree "
              "field of view), ")
    note = (f"Schematic plan from camera view {view} ({len(vehicles)} vehicle tracks): NOT calibrated, NOT to scale. "
            f"{camera}so it shows the layout and order of movement, not distances, speeds or an impact point. "
            "Declare the lane-marking standard or add a manual calibration for a measured reconstruction."
            + (f" Views {', '.join(map(str, others))} also have tracks but a different frame." if others else ""))
    return Reconstruction(
        incident_id=timeline.incident_id, generated_at=datetime.now(timezone.utc),
        scene=Scene(type="metric", units="schematic", extent_m=round(half, 1),
                    center_m=(round((max(xs) + min(xs)) / 2, 2), round((max(ys) + min(ys)) / 2, 2)),
                    frame_note=(f"schematic plan, camera view {view}: x = across the road, y = along the road, away from the camera, "
                                "road direction from its lane lines (not to scale)" if aligned is not None else
                                f"schematic plan, camera view {view}: x = across the road, y = away from the camera (not to scale)")),
        t_min_s=round(min(times), 3), t_max_s=round(max(times), 3), vehicles=vehicles, collision=None, note=note)
