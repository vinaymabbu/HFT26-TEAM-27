"""Module 1 — Evidence ingestion (dashcam or fixed-camera video -> observations).

OpenCV validates the clip and picks up to MAX_FRAMES keyframes: an even baseline plus
the moments of most visual change (video.select_keyframes, scanned at SAMPLE_FPS). The
Featherless vision model describes each keyframe as JSON (see vision.py), WORKERS calls
at a time; answers are cached per file/model/prompt/frame under storage/cache/vision,
so re-running the same video costs nothing. Per-frame findings
are collapsed into state changes (a signal turning yellow, the ego vehicle
starting to brake, a collision) and returned as Observations with
`t_rel_s = frame / fps + offset` and `frame:<n>` locators.

Camera mode (VIDEO_CAMERA_MODE=auto|dashcam|fixed, default auto): a dashcam has an
ego vehicle; fixed roadside cameras (CCTV, possibly a grid of views) do not, and only
notable vehicles, hazards, visible signals and collisions are reported. "auto" decides
from how much of the background changes between sampled frames (see video.py).

With no FEATHERLESS_API_KEY the video is still validated and sampled, then
NotImplementedError is raised so the pipeline skips the stage.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app import config
from app.schemas import Evidence, Observation, ObservationSet, SourceRef, SourceType

from .video import (MOVING_CAMERA_SHARE, Frame, Track, VideoInfo, camera_motion_share, detect_grid, ego_events,
                    ego_motion, encode_jpeg_b64, extract_frames, probe_video, select_keyframes, static_regions,
                    track_vehicles, with_followers)
from . import calibration
from .linking import Sight, histogram, kind_of, link_sightings, stationary_run
from .road_geometry import estimate_vanishing_point, traffic_agrees
from .vision import PROMPTS, CameraMode, FrameAnalysis, VisionClient, VisionError

# Ungated on Featherless; google/gemma-3-27b-it is gated (needs HuggingFace OAuth, else 403).
VISION_MODEL = os.environ.get("FEATHERLESS_VISION_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct")
SAMPLE_FPS = float(os.environ.get("VIDEO_SAMPLE_FPS", "4"))   # cheap OpenCV scan rate for keyframe picking
MAX_FRAMES = int(os.environ.get("VIDEO_MAX_FRAMES", "24"))    # vision-model calls per video
# Parallel model calls. Featherless plans cap concurrency (Qwen3-VL-30B costs 2 of a plan's units; pay-per-request plans allow 100); 429s are retried.
WORKERS = int(os.environ.get("VIDEO_WORKERS", "24"))  # = MAX_FRAMES: one round of calls
# Seconds between the incident reference time and frame 0 (dashcam clock offset).
T0_OFFSET_S = float(os.environ.get("VIDEO_T0_OFFSET_S", "0"))
def ego_vehicle_id() -> str:
    """The case's dashcam vehicle (declared with ego: true), else the neutral id "EGO"."""
    from app import config
    return next((v["id"] for v in config.CASE_VEHICLES if v.get("ego")), None) or \
        os.environ.get("VIDEO_EGO_VEHICLE_ID", "EGO")
MIN_CONFIDENCE = 0.2
CAMERA_MODE = os.environ.get("VIDEO_CAMERA_MODE", "auto").strip().lower()
TRACKING = os.environ.get("VIDEO_TRACKING", "1") != "0"  # fixed cameras: OpenCV motion tracks
EGO_MOTION = os.environ.get("VIDEO_EGO_MOTION", "1") != "0"  # dashcams: optical-flow ego motion
MAX_TRACKS = int(os.environ.get("VIDEO_MAX_TRACKS", "200"))
PAIR_GAP_S = float(os.environ.get("VIDEO_PAIR_GAP_S", "1.0"))  # fixed cameras: second image this much later
# Fixed cameras: the model often misreads flowing traffic as "stopped", so every stopped vehicle must
# come with a box and that patch of the image must stay unchanged a few seconds later (video.static_regions).
# Momentary actions can't be judged from snapshots seconds apart, so fixed mode drops them.
TRANSIENT_ACTIONS = {"braking", "accelerating", "changing_lanes", "turning", "moving",
                     "entering_intersection", "crossing_intersection"}
TRANSIENT_HAZARDS = {"sudden_braking", "swerve", "other"}


def detect_camera_mode(frames: list[Frame]) -> tuple[CameraMode, str]:
    """(mode, how it was decided) from VIDEO_CAMERA_MODE or the background change between frames."""
    if CAMERA_MODE in ("dashcam", "fixed"):
        return CAMERA_MODE, f"set by VIDEO_CAMERA_MODE={CAMERA_MODE}"
    share = camera_motion_share(frames)
    if share is None:
        return "dashcam", "too few frames to judge; assumed dashcam"
    mode: CameraMode = "dashcam" if share > MOVING_CAMERA_SHARE else "fixed"
    return mode, (f"auto-detected: {share:.0%} of the frame changes between samples "
                  f"({'>' if mode == 'dashcam' else '<='} {MOVING_CAMERA_SHARE:.0%} threshold)")


def load_frames(evidence: Evidence) -> tuple[VideoInfo, list[Frame]]:
    """Validate the video and return its keyframes. Raises VideoError if unusable."""
    info = probe_video(evidence.stored_path)
    frames = extract_frames(evidence.stored_path, info,
                            select_keyframes(evidence.stored_path, info, MAX_FRAMES, SAMPLE_FPS))
    return info, frames


class _Cache:
    """Per-frame model answers on disk, keyed by file content, model, prompt and frame index."""

    def __init__(self, evidence: Evidence, model: str, mode: CameraMode, pair_gap_s: float = 0.0):
        h = hashlib.sha256()
        with open(evidence.stored_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        prompt = hashlib.sha256(PROMPTS[mode].encode()).hexdigest()[:8]
        safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
        self.dir = Path(config.STORAGE_DIR) / "cache" / "vision" / h.hexdigest()[:16] / (f"{safe_model}_{mode}_{prompt}" + (f"_pair{pair_gap_s:g}" if pair_gap_s else ""))

    def get(self, frame: Frame) -> FrameAnalysis | None:
        path = self.dir / f"{frame.index}.json"
        try:
            return FrameAnalysis.model_validate_json(path.read_text()) if path.exists() else None
        except ValueError:
            return None

    def put(self, frame: Frame, analysis: FrameAnalysis) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{frame.index}.json").write_text(analysis.model_dump_json())


def analyze_video(evidence: Evidence, client: VisionClient | None = None,
                  progress: Callable[[int, int, str], None] | None = None) -> ObservationSet:
    """Return observations with timestamps and frame-level source refs.

    `progress(done, total, note)` is called as keyframes are analysed (for the dashboard).
    """
    report = progress or (lambda done, total, note: None)
    report(0, 0, "scanning video for keyframes")
    info, frames = load_frames(evidence)

    # Judge the camera on consecutive frames: keyframes are seconds apart, so traffic alone
    # would change most of the picture between them and every camera would look like a dashcam.
    step = max(1, round(info.fps / SAMPLE_FPS))
    burst = extract_frames(evidence.stored_path, info, [i * step for i in range(min(16, info.frame_count // step))])
    mode, why = detect_camera_mode(burst or frames)
    warnings: list[str] = [f"Camera mode: {mode} ({why})."]
    if T0_OFFSET_S == 0:
        warnings.append(f"{'Dashcam' if mode == 'dashcam' else 'Camera'} clock offset assumed 0 s relative to "
                        "the incident reference time (not verified).")

    # Fixed cameras: vehicle trajectories from the pixels themselves (OpenCV only, no model, no key).
    track_obs: list[Observation] = []
    roads: dict[int, dict] = {}
    cals: dict = {}
    views: list[tuple[int, int, int, int]] = []
    if mode == "fixed" and TRACKING and frames:
        report(0, 0, "tracking vehicles")
        views = detect_grid(frames[0].image)
        backgrounds: dict = {}
        tracks = track_vehicles(evidence.stored_path, info, views, backgrounds=backgrounds)
        report(0, 0, "calibrating cameras")
        cals = calibrate_views(evidence, views, backgrounds)
        try:  # an enhancement: whatever goes wrong here, the tracks themselves still stand
            roads = estimate_roads(views, backgrounds, tracks, warnings)
        except Exception as e:  # noqa: BLE001
            roads = {}
            warnings.append(f"Road direction could not be estimated ({type(e).__name__}: {e}); the schematic plan assumes a nominal camera.")
        track_obs = tracks_to_observations(evidence, info, tracks, views, cals, roads)
        warnings.append(f"{len(track_obs)} vehicle track(s) from motion in {len(views)} camera view(s) "
                        "(stationary vehicles are not tracked by motion).")
        for v, cal in sorted(cals.items()):
            if cal.ok:
                warnings.append(f"Camera view {v} calibrated ({cal.method.replace('_', ' ')}): {cal.reason}.")
            else:
                warnings.append(f"Camera view {v} not calibrated: {cal.reason}. Speeds unavailable; set a manual "
                                f"calibration with PUT /api/evidence/{evidence.id}/calibration/{v}.")

    # Dashcams: the camera car's own motion from optical flow (OpenCV only, no model, no key).
    ego_obs: list[Observation] = []
    if mode == "dashcam" and EGO_MOTION:
        report(0, 0, "measuring ego motion")
        ego_obs = ego_to_observations(evidence, info, ego_events(ego_motion(evidence.stored_path, info)))
        warnings.append(f"Ego motion of {ego_vehicle_id()} from optical flow: {len(ego_obs)} change(s). Relative "
                        "(no camera calibration): moving / braking / stopped / turning, not km/h.")
    coverage = (round(T0_OFFSET_S, 3), round(T0_OFFSET_S + info.duration_s, 3))

    if client is None:
        if not config.FEATHERLESS_API_KEY:
            if track_obs or ego_obs:
                warnings.append("FEATHERLESS_API_KEY is not set: motion analysis only, no vision-model findings.")
                return ObservationSet(source_type=SourceType.video, evidence_id=evidence.id,
                                      producer="evidence_ingestion:opencv",
                                      generated_at=datetime.now(timezone.utc), observations=track_obs + ego_obs,
                                      warnings=warnings, coverage_s=coverage)
            raise NotImplementedError(
                f"video valid ({info.frame_count} frames @ {info.fps:.1f} fps, {len(frames)} sampled) "
                "but FEATHERLESS_API_KEY is not set; no vision analysis"
            )
        client = VisionClient(config.FEATHERLESS_API_KEY, config.FEATHERLESS_BASE_URL, VISION_MODEL)
        owns_client = True
    else:
        owns_client = False

    if mode == "fixed" and PAIR_GAP_S > 0:
        frames = with_followers(evidence.stored_path, info, frames, PAIR_GAP_S)
    warnings.append(f"{len(frames)} keyframes analysed out of {info.frame_count} frames "
                    f"({info.duration_s:.0f} s @ {info.fps:g} fps).")
    model = getattr(client, "model", VISION_MODEL)
    cache = _Cache(evidence, model, mode, PAIR_GAP_S if mode == "fixed" else 0.0)

    def describe(frame: Frame) -> tuple[Frame, FrameAnalysis | None, str | None]:
        if (hit := cache.get(frame)) is not None:
            return frame, hit, None
        try:
            if frame.next_image is not None:
                analysis = client.describe_frame(encode_jpeg_b64(frame.image), frame.t_s, mode=mode,
                                                 next_jpeg_b64=encode_jpeg_b64(frame.next_image), gap_s=frame.next_gap_s)
            else:
                analysis = client.describe_frame(encode_jpeg_b64(frame.image), frame.t_s, mode=mode)
        except VisionError as e:
            return frame, None, str(e)
        cache.put(frame, analysis)
        return frame, analysis, None

    results = []
    report(0, len(frames), f"{mode} mode: analysing {len(frames)} keyframes")
    try:
        with ThreadPoolExecutor(max_workers=max(1, WORKERS)) as pool:
            for fut in as_completed([pool.submit(describe, f) for f in frames]):
                results.append(fut.result())
                report(len(results), len(frames), f"{mode} mode: analysed {len(results)}/{len(frames)} keyframes")
    finally:
        if owns_client:
            client.close()

    failed = [err for _, a, err in results if a is None]
    if frames and len(failed) == len(frames):
        raise RuntimeError(f"vision model failed on all {len(frames)} frames; first error: {failed[0]}")
    if failed:
        warnings.append(f"Vision model failed on {len(failed)}/{len(frames)} frames; those frames were skipped.")

    analysed = [(f, a) for f, a, _ in results if a is not None]
    persistence: dict = {}
    raw = analysed  # every sighting as the model gave it; aggregation below keeps only what the pixels confirm
    if mode == "fixed":
        analysed, persistence, dropped = aggregate_fixed(
            analysed, lambda checks: static_regions(evidence.stored_path, info, checks))
        if dropped["stopped"]:
            warnings.append(f"{dropped['stopped']} 'stopped' report(s) dropped: the vehicle's patch of the image "
                            "changed 2-4 s later (it was moving) or no box was given to check.")
        if dropped["transient"]:
            warnings.append(f"{dropped['transient']} momentary claim(s) (braking, lane changes, swerves) dropped: "
                            "they can't be judged reliably from keyframes seconds apart.")
    observations = frames_to_observations(evidence, info, analysed, mode, persistence, raw=raw) + track_obs + ego_obs
    if mode == "dashcam":
        observations += box_tracks(evidence, info, analysed)
    elif mode == "fixed" and len(views) == 1 and len(track_obs) < FEW_MOTION_TRACKS:
        try:
            boxed, stops = fixed_box_observations(evidence, info, raw, track_obs, roads.get(1), stops_known=observations, cal=cals.get(1))
        except Exception as e:  # noqa: BLE001
            boxed, stops = [], []
            warnings.append(f"Linking the vision model's vehicle boxes failed ({type(e).__name__}: {e}); no AI-sighted tracks were added.")
        if boxed:
            observations += boxed + stops
            warnings.append(f"Motion tracking found only {len(track_obs)} vehicle(s), so {len(boxed)} more coarse track(s) "
                            "were linked from the vision model's vehicle boxes (inferred; keyframes are seconds apart)"
                            + (f"; {len(stops)} vehicle(s) that stayed at one place across keyframes are reported as stopped." if stops else "."))
    return ObservationSet(
        source_type=SourceType.video,
        evidence_id=evidence.id,
        producer=f"evidence_ingestion:{model}",
        generated_at=datetime.now(timezone.utc),
        observations=observations,
        warnings=warnings,
        coverage_s=coverage,
    )


def ego_to_observations(evidence: Evidence, info: VideoInfo, events: list[dict]) -> list[Observation]:
    out = []
    for n, e in enumerate(events, start=1):
        t = round(e["t"] + T0_OFFSET_S, 3)
        attrs = {k: v for k, v in e.items() if k not in ("t", "action")} | {"ego": True, "method": "optical flow"}
        out.append(Observation(
            id=f"vid_{evidence.id}_ego_{n:03d}", source_type=SourceType.video,
            source_ref=SourceRef(evidence_id=evidence.id, file=evidence.filename,
                                 locator=f"frame:{round(e['t'] * info.fps)}"),
            timestamp=config.INCIDENT_REFERENCE_TIME + timedelta(seconds=t), t_rel_s=t,
            time_uncertainty_s=0.3, vehicle_id=ego_vehicle_id(), vehicle_description="camera vehicle (dashcam)",
            action=e["action"], attributes=attrs, basis="derived",
            confidence=0.75 if e["action"] in ("braking", "stopped", "moving") else 0.6))
    return out


# "collided", "crashed into", "hit by", "struck"; but not a crash barrier / cushion
COLLISION_WORDS = re.compile(r"\b(collid\w*|crash(?:ed|es|ing)?(?!\s+(?:barrier|cushion|attenuator|test))|hit by|struck|smash(?:ed|es)|rear-ended)\b")
FEW_MOTION_TRACKS = 3     # fewer motion tracks than this in a one-view scene: add the model's boxes as tracks
BOX_DUPLICATE_PX = 150.0  # a box track this close (and in time) to a motion track is the same vehicle


def box_tracks(evidence: Evidence, info: VideoInfo, analysed: list[tuple[Frame, FrameAnalysis]],
               max_jump: float = 180.0, max_gap_s: float = 3.0, fixed: bool = False,
               existing: list[Observation] | None = None, road: dict | None = None) -> list[Observation]:
    """Link the vision model's boxes across keyframes into coarse image-space tracks, basis "inferred" (the boxes are a
    model's and keyframes are seconds apart).

    Fixed cameras (`fixed=True`): absolute image positions, the box's bottom centre where the vehicle meets the road,
    linked by position, size, colour and label together with the road's direction (linking.py), used when motion
    tracking found few vehicles; boxes that duplicate a motion track in `existing` are skipped.
    Dashcams: camera-relative (the camera itself moves), linked by the same-looking label alone.
    """
    if fixed:
        return fixed_box_observations(evidence, info, analysed, existing or [], road)[0]
    tracks: list[dict] = []
    for frame, a in sorted(analysed, key=lambda fa: fa[0].t_s):
        for v in a.vehicles:
            if not v.box or v.confidence < MIN_CONFIDENCE:
                continue
            cx, cy = (v.box[0] + v.box[2]) / 2, (v.box[1] + v.box[3]) / 2
            best = min((tr for tr in tracks if tr["key"] == _key(v.label) and frame.t_s - tr["pts"][-1][0] <= max_gap_s
                        and math.hypot(cx - tr["pts"][-1][1], cy - tr["pts"][-1][2]) <= max_jump),
                       key=lambda tr: math.hypot(cx - tr["pts"][-1][1], cy - tr["pts"][-1][2]), default=None)
            if best is None:
                best = {"key": _key(v.label), "label": v.label, "pts": [], "first": frame.index}
                tracks.append(best)
            best["pts"].append((frame.t_s, cx, cy))
            best["last"] = frame.index
    out = []
    for n, tr in enumerate((tr for tr in tracks if len(tr["pts"]) >= 2), start=1):
        pts = [[round(t + T0_OFFSET_S, 3), round(x / 1000 * info.width, 1), round(y / 1000 * info.height, 1)]
               for t, x, y in tr["pts"]]
        out.append(Observation(
            id=f"vid_{evidence.id}_box_{n:03d}", source_type=SourceType.video,
            source_ref=SourceRef(evidence_id=evidence.id, file=evidence.filename,
                                 locator=f"frames:{tr['first']}-{tr['last']}"),
            timestamp=config.INCIDENT_REFERENCE_TIME + timedelta(seconds=pts[0][0]), t_rel_s=pts[0][0],
            time_uncertainty_s=0.5, vehicle_description=tr["label"], action="track", basis="inferred",
            attributes={"track_id": f"T1-{n:03d}", "view": 1, "camera": "dashcam", "units": "px",
                        "frame_size": [info.width, info.height], "fps": round(info.fps, 3),
                        "view_box": [0, 0, info.width, info.height], "grid": [[0, 0, info.width, info.height]],
                        "label": tr["label"], "points": pts, "source": "AI vehicle boxes linked across keyframes"},
            confidence=0.5))
    return out


def fixed_box_observations(evidence: Evidence, info: VideoInfo, analysed: list[tuple[Frame, FrameAnalysis]],
                           existing: list[Observation], road: dict | None,
                           stops_known: list[Observation] | None = None, cal=None) -> tuple[list[Observation], list[Observation]]:
    """(tracks, stops) for a fixed camera from the vision model's boxes (see linking.py).

    One track per vehicle seen in at least two keyframes. A vehicle that moved and then stayed at one place across
    several keyframes also gives a "stopped" observation, with the keyframes that show it; vehicles that never moved
    (parked) are not incident events, and a stop the pixel check already confirmed (`stops_known`) is not repeated.
    With a calibrated view (`cal`) each track also carries its positions on the road in metres (`ground_points`), as
    inferred positions: the model's boxes are seconds apart, so no speeds are derived from them."""
    frames = []
    for frame, a in sorted(analysed, key=lambda fa: fa[0].t_s):
        sights = []
        for v in a.vehicles:
            if not v.box or v.confidence < MIN_CONFIDENCE or kind_of(v.label) == "person":
                continue  # a pedestrian is not a vehicle track
            box = (v.box[0] / 1000 * info.width, v.box[1] / 1000 * info.height,
                   v.box[2] / 1000 * info.width, v.box[3] / 1000 * info.height)
            sights.append(Sight(frame.t_s, v.label, box, v.confidence, histogram(frame.image, box), frame.index, v.direction))
        frames.append((frame.t_s, sights))
    vp = tuple(road["vanishing_point_px"]) if road and road.get("vanishing_point_px") else None
    known = [(p[0], p[1], p[2]) for o in existing if o.action == "track" for p in o.attributes.get("points", [])]

    def duplicate(pts) -> bool:
        near = sum(any(abs(t - kt) <= 0.6 and math.hypot(x - kx, y - ky) <= BOX_DUPLICATE_PX for kt, kx, ky in known)
                   for t, x, y in pts)
        return bool(known) and near >= max(1, len(pts) / 2)

    out, stops = [], []
    for tr in (t for t in link_sightings(frames, info.width, vp) if len(t.sights) >= 2):
        pts = [[round(s.t + T0_OFFSET_S, 3), round(s.contact[0], 1), round(s.contact[1], 1)] for s in tr.sights]
        if duplicate(pts):
            continue
        n = len(out) + 1
        stop = stationary_run(tr, info.width)
        if stop is not None and stop[0] > 0:  # it moved first: a stop is something that happened
            stops.append(_stop_observation(evidence, info, tr, stop, len(stops) + 1, stops_known or [], f"U-B1-{n:03d}"))
        labels: dict[str, int] = {}
        for s in tr.sights:
            labels[s.label] = labels.get(s.label, 0) + 1
        direction = next((s.direction for s in reversed(tr.sights) if s.direction), None)
        metric = {}
        if cal is not None and cal.ok:
            ground = []
            for t, x, y in pts:
                g = cal.ground(x, y) if cal.usable(y, info.height) and y < info.height - 3 else None
                if g is not None:
                    ground.append([t, None if g[0] is None else round(g[0], 2), round(g[1], 2)])
            if len(ground) >= 3:
                metric = {"calibration": cal.method, "ground_points": ground}
        out.append(Observation(
            id=f"vid_{evidence.id}_box_{n:03d}", source_type=SourceType.video,
            source_ref=SourceRef(evidence_id=evidence.id, file=evidence.filename,
                                 locator=f"frames:{tr.sights[0].index}-{tr.sights[-1].index}"),
            timestamp=config.INCIDENT_REFERENCE_TIME + timedelta(seconds=pts[0][0]), t_rel_s=pts[0][0],
            time_uncertainty_s=0.5, vehicle_description=tr.label, action="track", basis="inferred",
            attributes={"track_id": f"B1-{n:03d}", "view": 1, "camera": "fixed", "units": "px",
                        "frame_size": [info.width, info.height], "fps": round(info.fps, 3),
                        "view_box": [0, 0, info.width, info.height], "grid": [[0, 0, info.width, info.height]],
                        "label": tr.label, "labels": dict(sorted(labels.items(), key=lambda kv: -kv[1])), "points": pts,
                        "boxes": [[round(v, 1) for v in s.box] for s in tr.sights],
                        "sightings": len(tr.sights), "source": "AI vehicle boxes linked across keyframes",
                        **({"direction": direction} if direction else {}), **(road or {}), **metric},
            confidence=round(min(0.45, 0.3 + 0.04 * len(tr.sights)), 2)))
    return out, [o for o in stops if o is not None]


def _stop_observation(evidence: Evidence, info: VideoInfo, tr, run: tuple[int, int], n: int,
                      known: list[Observation], vehicle_id: str) -> Observation | None:
    i, j = run
    first, last = tr.sights[i], tr.sights[j]
    mid = tr.sights[(i + j) // 2]
    cx, cy = mid.contact[0] / info.width * 1000, mid.contact[1] / info.height * 1000
    t0, t1 = first.t + T0_OFFSET_S, last.t + T0_OFFSET_S
    for o in known:  # the pixel check already confirmed this vehicle at this spot
        b = o.attributes.get("box")
        if (o.action == "stopped" and b and abs((b[0] + b[2]) / 2 - cx) <= 80 and abs(b[3] - cy) <= 80
                and o.attributes.get("first_seen_s", o.t_rel_s or 0.0) <= t1 + 0.5 and o.attributes.get("last_seen_s", o.t_rel_s or 0.0) >= t0 - 0.5):
            return None
    box = [round(v, 1) for v in (mid.box[0] / info.width * 1000, mid.box[1] / info.height * 1000,
                                 mid.box[2] / info.width * 1000, mid.box[3] / info.height * 1000)]
    before = tr.sights[i - 1].t if i > 0 else first.t
    count = j - i + 1
    direction = next((s.direction for s in reversed(tr.sights) if s.direction), None)
    return Observation(
        id=f"vid_{evidence.id}_stop_{n:03d}", source_type=SourceType.video,
        source_ref=SourceRef(evidence_id=evidence.id, file=evidence.filename, locator=f"frame:{first.index}"),
        timestamp=config.INCIDENT_REFERENCE_TIME + timedelta(seconds=t0), t_rel_s=round(t0, 3),
        # it stopped somewhere between the keyframe before and the first one that shows it standing
        time_uncertainty_s=round(max(0.5, (first.t - before) / 2 if i > 0 else 0.5), 3),
        vehicle_id=vehicle_id,  # the linked track's identity, so two white cars stay two vehicles on the timeline
        vehicle_description=f"{tr.label} stopped", action="stopped",
        attributes={"label": tr.label, "box": box, "frame_size": [info.width, info.height], "camera": "fixed", "view": 1,
                    "seen_in_keyframes": count, "first_seen_s": round(t0, 2), "last_seen_s": round(t1, 2),
                    "verified_by": f"at the same place in {count} keyframes over {last.t - first.t:.1f} s",
                    **({"direction": direction} if direction else {})},
        confidence=round(min(0.7, 0.4 + 0.05 * count), 2))


def calibration_key(evidence: Evidence) -> str:
    return evidence.sha256 or evidence.id


def calibrate_views(evidence: Evidence, views: list[tuple[int, int, int, int]],
                    backgrounds: dict) -> dict[int, "calibration.ViewCalibration"]:
    """Per view: the saved manual calibration if any, else the automatic lane-dash one (saved for the API)."""
    saved = calibration.load(calibration_key(evidence))
    out, auto = {}, {}
    for v, box in enumerate(views, start=1):
        if (m := saved.get("manual", {}).get(str(v))) is not None:
            out[v] = calibration.from_dict(m)
            continue
        bg = backgrounds.get(v)
        cal = (calibration.auto_calibrate(bg, v, box) if bg is not None
               else calibration.ViewCalibration(v, "lane_dashes", False, "no background image"))
        out[v] = cal
        auto[str(v)] = cal.to_dict()
    saved["auto"] = auto
    calibration.save(calibration_key(evidence), saved)
    return out


def estimate_roads(views: list[tuple[int, int, int, int]], backgrounds: dict, tracks: list[Track],
                   warnings: list[str]) -> dict[int, dict]:
    """Per view: where the road's lane lines, rails and kerbs meet (the vanishing point), in full-frame pixels, with the
    quality of the evidence. It fixes the road's direction for the plan view; it is not a calibration (no metres). A view
    is left out, with the reason in `warnings`, when the picture does not support it or the traffic does not follow it."""
    roads: dict[int, dict] = {}
    for v, (x0, y0, x1, y1) in enumerate(views, start=1):
        bg = backgrounds.get(v)
        if bg is None:
            continue
        vp, why = estimate_vanishing_point(bg)
        if vp is None:
            warnings.append(f"Camera view {v}: road direction not found ({why}); the schematic plan assumes a nominal camera.")
            continue
        local = [[(t, x - x0, y - y0) for t, x, y in tr.points] for tr in tracks if tr.view == v]
        agrees, how = traffic_agrees((vp.x, vp.y), local, y1 - y0)
        if not agrees:
            warnings.append(f"Camera view {v}: lane lines suggest a road direction, but the traffic does not follow it "
                            f"({how}); the schematic plan assumes a nominal camera.")
            continue
        roads[v] = {"vanishing_point_px": [round(vp.x + x0, 1), round(vp.y + y0, 1)],
                    "road_geometry": {**vp.to_dict(), "traffic_check": how}}
        warnings.append(f"Camera view {v}: road direction found from {vp.distinct_lines} lane lines / rails "
                        f"(vanishing point at {vp.x + x0:.0f}, {vp.y + y0:.0f} px); used to straighten the schematic plan.")
    return roads


def tracks_to_observations(evidence: Evidence, info: VideoInfo, tracks: list[Track],
                           views: list[tuple[int, int, int, int]],
                           cals: dict | None = None, roads: dict | None = None) -> list[Observation]:
    """One compact observation per track (action "track"): all its points ride in attributes.points.
    Calibrated views add ground_points [[t, X_m, Y_m]], speed_points [[t, km/h]] and mean_speed_kmh."""
    longest = sorted(tracks, key=lambda tr: tr.points[-1][0] - tr.points[0][0], reverse=True)[:MAX_TRACKS]
    out = []
    for n, tr in enumerate(sorted(longest, key=lambda tr: (tr.points[0][0], tr.view)), start=1):
        (t0, x0, y0), (t1, x1, y1) = tr.points[0], tr.points[-1]
        dist = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(tr.points, tr.points[1:]))
        metric = {}
        cal = (cals or {}).get(tr.view)
        if cal is not None and cal.ok:
            ground, speeds, mean = calibration.track_speeds(cal, [list(p) for p in tr.points], views[tr.view - 1][3])
            if mean is not None:
                metric = {"calibration": cal.method, "mean_speed_kmh": mean,
                          "speed_points": [[round(t + T0_OFFSET_S, 3), s] for t, s in speeds.items()],
                          "ground_points": [[round(g[0] + T0_OFFSET_S, 3), g[1], g[2]] for g in ground]}
        out.append(Observation(
            id=f"vid_{evidence.id}_trk_{n:03d}",
            source_type=SourceType.video,
            source_ref=SourceRef(evidence_id=evidence.id, file=evidence.filename,
                                 locator=f"frames:{tr.first_frame}-{tr.last_frame}"),
            timestamp=config.INCIDENT_REFERENCE_TIME + timedelta(seconds=t0 + T0_OFFSET_S),
            t_rel_s=round(t0 + T0_OFFSET_S, 3),
            time_uncertainty_s=round(1 / info.fps, 3),
            vehicle_id=None,
            vehicle_description=f"moving vehicle, camera view {tr.view}",
            action="track",
            basis="derived",  # OpenCV motion tracking: deterministic, not a model's reading
            attributes={"track_id": f"T{tr.view}-{n:03d}", "view": tr.view, "camera": "fixed", "units": "px",
                        "frame_size": [info.width, info.height], "fps": round(info.fps, 3),
                        "view_box": list(views[tr.view - 1]), "grid": [list(v) for v in views],
                        "direction": "towards camera" if y1 > y0 else "away from camera",
                        "mean_speed_px_s": round(dist / max(t1 - t0, 1e-6), 1),
                        "points": [[round(t + T0_OFFSET_S, 3), x, y] for t, x, y in tr.points],
                        **({"boxes": [[round(v, 1) for v in b] for b in tr.boxes]} if tr.boxes else {}), **metric,
                        **(roads or {}).get(tr.view, {})},
            confidence=0.6,
        ))
    return out


def view_of_box(box: list[float] | None, views: int | None) -> int | None:
    """Camera view (1-based, left-to-right, top-to-bottom) containing the box centre, for a grid of
    `views` cameras laid out as near-square as possible (4 -> 2x2, 2 -> 2x1, 6 -> 3x2)."""
    if not box or not views or views < 2:
        return None
    cols = math.ceil(math.sqrt(views))
    rows = math.ceil(views / cols)
    cx, cy = (box[0] + box[2]) / 2000, (box[1] + box[3]) / 2000
    view = min(rows - 1, int(cy * rows)) * cols + min(cols - 1, int(cx * cols)) + 1
    return view if view <= views else None


def aggregate_fixed(analysed: list[tuple[Frame, FrameAnalysis]],
                    verify: Callable[[list[tuple[int, list[float]]]], list[dict]] | None = None):
    """Fixed cameras: keep what the pixels confirm, drop what can't be judged from snapshots.

    * stopped vehicles: kept only if they come with a box and `verify` finds that patch static
      2-4 s later. The same spot seen in several keyframes becomes one vehicle (first label wins).
    * stopped_in_lane hazards: folded into the verified stopped vehicles (dropped as separate items)
    * momentary vehicle actions and hazards (braking, lane change, swerve, "other"): dropped
    * other hazards (wrong way, debris, pedestrian): kept if reported in >= 2 keyframes of that view
    * collisions: always kept
    Returns (filtered analyses, {(view, label key): (sightings, first_t, last_t)}, dropped counts).
    """
    timed = sorted(analysed, key=lambda fa: fa[0].t_s)
    # The model's view numbers slip; where a box sits in the camera grid does not.
    timed = [(f, a.model_copy(update={"vehicles": [
        v.model_copy(update={"view": view_of_box(v.box, a.views) or v.view}) if v.box else v for v in a.vehicles]}))
        for f, a in timed]
    stops = [(f, v) for f, a in timed for v in a.vehicles if v.action == "stopped" and v.box]
    verdicts = verify([(f.index, v.box) for f, v in stops]) if (verify and stops) else [{"static": False}] * len(stops)
    confirmed = {id(v) for (_, v), r in zip(stops, verdicts) if r.get("static")}

    # One identity per stopped spot: same view, box centres within 80/1000 of each other.
    spots: list[tuple[int | None, float, float, str]] = []
    persistent: dict[tuple, list[float]] = {}

    def spot_label(v) -> str:
        cx, cy = (v.box[0] + v.box[2]) / 2, (v.box[1] + v.box[3]) / 2
        for view, x, y, label in spots:
            if view == v.view and abs(x - cx) <= 80 and abs(y - cy) <= 80:
                return label
        spots.append((v.view, cx, cy, v.label))
        return v.label

    hazard_seen: dict[tuple, int] = {}
    for _, a in timed:
        for h in a.hazards:
            hazard_seen[(h.view, h.type)] = hazard_seen.get((h.view, h.type), 0) + 1

    out, dropped = [], {"stopped": 0, "transient": 0}
    for f, a in timed:
        keep_v = []
        for v in a.vehicles:
            if v.action == "stopped":
                if id(v) in confirmed:
                    v = v.model_copy(update={"label": spot_label(v)})
                    persistent.setdefault((v.view, _key(v.label)), []).append(f.t_s)
                    keep_v.append(v)
                else:
                    dropped["stopped"] += 1
            elif v.action in TRANSIENT_ACTIONS:
                dropped["transient"] += 1
            else:
                keep_v.append(v)
        keep_h = []
        for h in a.hazards:
            if h.type == "stopped_in_lane":
                continue  # represented by the verified stopped vehicle itself
            if h.type in TRANSIENT_HAZARDS:
                dropped["transient"] += 1
            elif hazard_seen[(h.view, h.type)] >= 2:
                keep_h.append(h)
        out.append((f, a.model_copy(update={"vehicles": keep_v, "hazards": keep_h})))
    summary = {k: (len(ts), min(ts), max(ts)) for k, ts in persistent.items()}
    return out, summary, dropped


def _key(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def frames_to_observations(
    evidence: Evidence, info: VideoInfo, analysed: list[tuple[Frame, FrameAnalysis]], mode: CameraMode = "dashcam",
    persistence: dict | None = None, raw: list[tuple[Frame, FrameAnalysis]] | None = None,
) -> list[Observation]:
    """Collapse per-frame findings into state changes; one Observation per change.

    `raw` is the same frames before fixed-camera filtering. Whether a vehicle is described as having collided does not
    depend on it being a confirmed-stopped vehicle, so that search uses the unfiltered sightings.

    Fixed mode: no ego vehicle (an "ego" answer is ignored), signals tied to an "ego lane" are
    dropped, plain "moving" traffic is not reported, and every observation carries its camera view.
    """
    fixed = mode == "fixed"
    # Half the sampling interval: the change happened somewhere since the previous sample.
    uncertainty = round(max(1.0 / SAMPLE_FPS, 1.0 / info.fps) / 2, 3)
    last_state: dict[tuple[str, str], str] = {}
    collision_seen = False
    gap_s = 0.0  # time since the previous analysed keyframe: the change happened somewhere in that gap
    out: list[Observation] = []

    def emit(frame: Frame, action: str, confidence: float, *, vehicle_id=None, description=None, attributes=None,
             at_or_before: bool = False) -> None:
        t_rel = round(frame.t_s + T0_OFFSET_S, 3)
        # Keyframes are picked adaptively and can be far apart, so the nominal rate understates the doubt. A claim of
        # something already visible ("a collision happened") may have happened any time since the last keyframe.
        spread = round(max(uncertainty, gap_s if at_or_before else gap_s / 2), 3)
        out.append(Observation(
            id=f"vid_{evidence.id}_{len(out) + 1:03d}",
            source_type=SourceType.video,
            source_ref=SourceRef(evidence_id=evidence.id, file=evidence.filename, locator=f"frame:{frame.index}"),
            timestamp=config.INCIDENT_REFERENCE_TIME + timedelta(seconds=t_rel),
            t_rel_s=t_rel,
            time_uncertainty_s=spread,
            vehicle_id=vehicle_id,
            vehicle_description=description,
            action=action,
            attributes=attributes or {},
            confidence=round(confidence, 3),
        ))

    def changed(key: tuple[str, str], state: str) -> bool:
        if last_state.get(key) == state:
            return False
        last_state[key] = state
        return True

    def with_view(attrs: dict, view) -> dict:
        if fixed:
            attrs["camera"] = "fixed"
            if view:
                attrs["view"] = view
        return attrs

    raw_by_index = {f.index: a for f, a in raw or []}
    prev_t: float | None = None
    for frame, a in sorted(analysed, key=lambda fa: fa[0].index):
        gap_s = 0.0 if prev_t is None else frame.t_s - prev_t
        prev_t = frame.t_s
        # Fixed cameras: a signal only counts if the model confirmed it can see a light head.
        for s in (a.signals if not fixed or a.signal_heads_visible else []):
            if s.state == "unknown" or s.confidence < MIN_CONFIDENCE:
                continue
            if fixed and "ego" in _key(s.approach):
                continue  # no ego lane on a fixed camera: a dashcam-style guess
            if changed(("signal", _key(s.approach), str(s.view if fixed else "")), s.state):
                emit(frame, "signal_state", s.confidence,
                     attributes=with_view({"approach": s.approach or "unknown", "signal_state": s.state}, s.view))

        if not fixed and a.ego and a.ego.confidence >= MIN_CONFIDENCE and changed(("vehicle", "ego"), a.ego.action):
            attrs = {"ego": True}
            if a.ego.cue:
                attrs["cue"] = a.ego.cue
            emit(frame, a.ego.action, a.ego.confidence, vehicle_id=ego_vehicle_id(),
                 description="ego vehicle (dashcam host)", attributes=attrs)

        for v in a.vehicles:
            if v.confidence < MIN_CONFIDENCE or not _key(v.label):
                continue
            key = ("vehicle", _key(v.label), str(v.view if fixed else ""))
            if fixed and v.action == "moving":
                last_state[key] = "moving"  # flowing traffic is not evidence, but a later change is
                continue
            if changed(key, v.action):
                attrs = {"label": v.label}
                if v.direction:
                    attrs["direction"] = v.direction
                if fixed and v.box:
                    attrs["box"] = v.box  # 0-1000 over the whole frame; lets the reconstruction place it
                    attrs["frame_size"] = [info.width, info.height]
                if (p := (persistence or {}).get((v.view, _key(v.label)))) and v.action == "stopped":
                    attrs.update(seen_in_keyframes=p[0], first_seen_s=round(p[1], 2), last_seen_s=round(p[2], 2))
                emit(frame, v.action, v.confidence, description=v.description or v.label,
                     attributes=with_view(attrs, v.view))

        for h in a.hazards if fixed else []:
            if h.confidence < MIN_CONFIDENCE:
                continue
            if changed(("hazard", h.type, str(h.view or "")), "present"):
                emit(frame, "hazard", h.confidence, description=h.description,
                     attributes=with_view({"hazard": h.type}, h.view))
        if fixed:  # a hazard that disappears can be reported again if it comes back
            seen = {("hazard", h.type, str(h.view or "")) for h in a.hazards if h.confidence >= MIN_CONFIDENCE}
            for k in [k for k in last_state if k[0] == "hazard" and k not in seen]:
                del last_state[k]

        c = a.collision
        if c and c.visible and c.confidence >= MIN_CONFIDENCE and not collision_seen:
            collision_seen = True
            vehicles = c.vehicles if fixed else [
                ego_vehicle_id() if _key(x) in {"ego", "ego vehicle", "dashcam"} else x for x in c.vehicles]
            attrs = {"vehicles": vehicles}
            if c.impact:
                attrs["impact_side"] = c.impact
            attrs["timing"] = "first keyframe where the AI reports a collision; it happened at or before this time"
            emit(frame, "collision", c.confidence, attributes=with_view(attrs, c.view), at_or_before=True)
        if not collision_seen:  # the dedicated field can lag: a vehicle described as having collided is evidence too
            for v in raw_by_index.get(frame.index, a).vehicles:
                if v.confidence >= MIN_CONFIDENCE and COLLISION_WORDS.search((v.description or "").lower()):
                    collision_seen = True
                    emit(frame, "collision", round(0.7 * v.confidence, 3), at_or_before=True, attributes=with_view({
                        "vehicles": [v.label], "label": v.label,
                        **({"box": v.box, "frame_size": [info.width, info.height]} if v.box else {}),
                        "timing": "first keyframe where a vehicle is described as having collided; it happened at or before this time",
                        "reported_in": f"vehicle description: {v.description}"}, v.view))
                    break
    return out
