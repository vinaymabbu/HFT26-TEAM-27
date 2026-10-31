"""Module 5 — Reconstruction: where the vehicles were, top-down, in metres.

Two outputs:
* build_reconstruction: the top-down map. From measured positions (sensor / GPS paths) when there are
  any; otherwise from camera tracks of a calibrated view projected onto the road plane; otherwise empty
  with a note saying what evidence is missing. Nothing is drawn without positional evidence.
* build_event_analysis: the camera footage with its detections and image-space tracks (pixels). That is
  evidence playback, not a reconstruction, and the dashboard shows it as "Event Analysis".
"""
from datetime import datetime, timezone

from app.schemas import ConflictReport, ObservationSet, Reconstruction, Scene, Timeline

from .builder import reconstruct
from .camera import reconstruct_from_camera, reconstruct_ground_from_camera, reconstruct_schematic_from_camera


def build_reconstruction(
    observation_sets: list[ObservationSet], timeline: Timeline, conflicts: ConflictReport
) -> Reconstruction:
    metric = reconstruct(observation_sets, timeline, conflicts)
    if metric.vehicles:
        return metric  # measured positions beat projected camera tracks
    ground = reconstruct_ground_from_camera(observation_sets, timeline)
    if ground is not None:
        return ground
    schematic = reconstruct_schematic_from_camera(observation_sets, timeline)
    if schematic is not None:
        return schematic  # an uncalibrated fixed camera: a labelled estimate beats an empty panel
    image_tracks = any(o.action == "track" and o.attributes.get("units") == "px"
                       for st in observation_sets for o in st.observations)
    has_video = any(st.source_type.value == "video" for st in observation_sets)
    camera_hint = ("Camera tracks exist but no view is calibrated: declare the lane-marking standard for the case "
                   "or set a manual calibration for a view. " if image_tracks else
                   "The video gives no positions on the road (e.g. a dashcam measures only its own relative motion). "
                   if has_video else "")
    return Reconstruction(
        incident_id=timeline.incident_id, generated_at=datetime.now(timezone.utc),
        scene=Scene(type="metric", extent_m=10), t_min_s=0.0, t_max_s=0.0, vehicles=[], collision=None,
        note="No positional evidence to reconstruct from. " + camera_hint
             + "Telemetry or GPS files with positions give a reconstruction.")


def build_event_analysis(observation_sets: list[ObservationSet], timeline: Timeline) -> Reconstruction | None:
    """Camera footage with detections and tracks in image pixels, or None without video tracks."""
    return reconstruct_from_camera(observation_sets, timeline)


FIXTURE_PREFIX = "ev_fx_"  # evidence ids of the bundled demo fixtures


def _provenance(rec: Reconstruction, observation_sets: list[ObservationSet] | None):
    """For each vehicle, a function t -> (observation_ids, basis). basis is `sensor` or `video` from the
    supporting observations, `simulated` for bundled demo data or for a position with nothing behind it.

    A position at a measured keyframe cites that observation; between two keyframes it cites both; with no
    keyframes (a tracked path) it cites the observations the track was built from."""
    by_id = {o.id: o for s in observation_sets or [] for o in s.observations}

    def basis_of(ids: list[str]) -> str:
        found = [by_id[i] for i in ids if i in by_id]
        if not found or all(o.source_ref.evidence_id.startswith(FIXTURE_PREFIX) for o in found):
            return "simulated"
        return "sensor" if any(o.source_type.value == "sensor" for o in found) else "video"

    gaps = [o for o in by_id.values() if o.action == "sensor_gap"]

    out = {}
    for v in rec.vehicles:
        cited = [i for i in v.source_observation_ids if i in by_id]
        keys: list[tuple[float, str]] = []
        for i in cited:
            o = by_id[i]
            if o.t_rel_s is not None and {"x_m", "y_m"} <= o.attributes.keys():
                keys.append((o.t_rel_s, i))
            for pt in o.attributes.get("points") or []:  # a path observation holds one sample per point: [t, x, y, ...]
                if isinstance(pt, (list, tuple)) and len(pt) >= 3:
                    keys.append((float(pt[0]), i))
        keys.sort()
        own_gaps = [g for g in gaps if g.vehicle_id in (None, v.vehicle_id)]

        def at(t: float, source: str | None, keys=keys, cited=cited, own_gaps=own_gaps) -> list[str]:
            if not keys:
                return cited[:3]
            exact = [i for kt, i in keys if abs(kt - t) < 0.01]
            if exact:
                ids = list(dict.fromkeys(exact))[:2]
            else:
                before = [i for kt, i in keys if kt < t]
                after = [i for kt, i in keys if kt > t]
                ids = list(dict.fromkeys(([before[-1]] if before else []) + ([after[0]] if after else [])))
            if source in ("interpolated", "extrapolated"):  # filled in: also cite the dropout that explains it
                ids += [g.id for g in own_gaps if g.attributes.get("gap_start_s", 1e9) - 0.01 <= t <= g.attributes.get("gap_end_s", -1e9) + 0.01]
            return ids

        out[v.vehicle_id] = lambda t, source=None, at=at: (lambda ids: (ids, basis_of(ids)))(at(t, source))
    return out


def positions_export(rec: Reconstruction, observation_sets: list[ObservationSet] | None = None) -> dict:
    """The reconstruction as a plain "id and positions" JSON: one list of timed positions per vehicle.

    Metres (+x east, +y north) for a top-down scene, pixels (+y down) for a camera scene. Each position says
    where it comes from: `source` (measured, interpolated, extrapolated, tracked, estimated), `basis` (sensor,
    video or simulated) and the `observation_ids` it rests on, so nothing reads as more certain than it is and
    every point can be traced back to evidence. Pass the run's observation sets to resolve the ids.
    """
    prov = _provenance(rec, observation_sets)
    px, sch = rec.scene.units == "px", rec.scene.units == "schematic"
    return {
        "incident_id": rec.incident_id,
        "generated_at": rec.generated_at.isoformat(),
        "coordinate_system": {
            "units": rec.scene.units,
            "x": "pixels from the left of the camera frame" if px else
                 "schematic units across the road (not to scale)" if sch else "metres east of the origin",
            "y": "pixels down from the top of the camera frame" if px else
                 "schematic units away from the camera (not to scale)" if sch else "metres north of the origin",
            "heading_deg": "0 = north, clockwise" if not px else "image-space",
            "time": "seconds from the incident reference time (t_rel_s)",
            "basis": "sensor | video | simulated (bundled demo data, or a position with no evidence behind it)",
            "scene": rec.scene.type,
            **({"warning": "estimate under stated assumptions: layout and order of movement only"} if sch else {}),
        },
        "t_min_s": rec.t_min_s,
        "t_max_s": rec.t_max_s,
        "vehicles": [
            {"id": v.vehicle_id, "label": v.label, "description": v.description,
             "positions": [{"t_s": p.t_s, "x": p.x_m, "y": p.y_m, "heading_deg": p.heading_deg,
                            "speed_kmh": p.speed_kmh, "source": p.source, "confidence": p.confidence, "seen_in_video": p.seen_in_video,
                            "basis": (pv := prov[v.vehicle_id](p.t_s, p.source))[1], "observation_ids": pv[0]}
                           for p in v.track],
             "source_observation_ids": v.source_observation_ids}
            for v in rec.vehicles],
        "collision": None if rec.collision is None else {
            "t_s": rec.collision.t_s, "x": rec.collision.x_m, "y": rec.collision.y_m,
            "confidence": rec.collision.confidence,
            "supporting_observation_ids": rec.collision.supporting_observation_ids},
        "note": rec.note,
    }
