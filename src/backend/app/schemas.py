"""Integration contract: the data shapes every module produces and consumes.

These models ARE the contract. Fixtures in /fixtures must validate against
them (tests/test_fixtures.py enforces this) and every real module must
return instances of them. Changing a field here is a contract change — see
docs/contracts/INTEGRATION_CONTRACT.md for the rules.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = "1.3.6"


# ---------------------------------------------------------------- evidence

class SourceType(str, Enum):
    video = "video"
    witness = "witness"
    sensor = "sensor"


class Evidence(BaseModel):
    """One uploaded file. Created by the upload endpoint, read by modules 1–2."""
    id: str
    ref: str | None = Field(None, description="Case-wide display id in upload order, e.g. EVID-002 (never reused)")
    kind: SourceType
    filename: str
    content_type: str | None = None
    size_bytes: int
    stored_path: str
    uploaded_at: datetime
    sha256: str | None = Field(None, description="Content hash taken at upload (chain of custody)")
    details: dict[str, Any] = Field(default_factory=dict, description="Upload-time inspection, e.g. video fps or CSV layout")


# ------------------------------------------------- modules 1–2: observations

class SourceRef(BaseModel):
    """Where in the original evidence an observation came from."""
    evidence_id: str
    file: str
    locator: str = Field(description="frame:<n> | line:<n> | row:<n> | sentence:<n>")


class Observation(BaseModel):
    """The common observation schema shared by video, witness and sensor."""
    id: str
    source_type: SourceType
    source_ref: SourceRef
    evidence_ref: str | None = Field(None, description="Human-readable pointer into the original evidence, e.g. 'EVID-002, line 3' or 'EVID-003, rows 412-415'; set at the end of a run")
    timestamp: datetime | None = Field(None, description="Absolute time if known")
    t_rel_s: float | None = Field(None, description="Seconds relative to incident reference time")
    time_uncertainty_s: float = Field(0.0, ge=0)
    vehicle_id: str | None = Field(None, description="Canonical id (V1, V2, ...) or null if unresolved")
    vehicle_description: str | None = None
    action: str = Field(description="e.g. moving, braking, entering_intersection, collision, signal_state, stopped")
    attributes: dict[str, Any] = Field(default_factory=dict, description="speed_kmh, heading_deg, signal_state, x_m, y_m, ...")
    confidence: float = Field(ge=0, le=1)
    raw_text: str | None = Field(None, description="Verbatim quote for witness-derived observations")
    basis: Literal["measured", "reported", "derived", "inferred"] | None = Field(
        None, description="measured: read from a sensor/log; reported: a person's statement; derived: computed "
                          "deterministically (tracking, calibration, fusion rules); inferred: an AI model's reading. "
                          "Defaults by source: sensor measured, witness reported, video inferred")
    derived_from: list[str] = Field(default_factory=list, description="Observation ids this one was computed from")

    @model_validator(mode="after")
    def _default_basis(self):
        if self.basis is None:
            self.basis = {SourceType.sensor: "measured", SourceType.witness: "reported",
                          SourceType.video: "inferred"}[self.source_type]
        return self


BASIS_RANK = {"measured": 0, "derived": 1, "reported": 2, "inferred": 3}


class ObservationSet(BaseModel):
    """Output of one extractor run over one piece of evidence."""
    schema_version: str = SCHEMA_VERSION
    source_type: SourceType
    evidence_id: str
    producer: str
    generated_at: datetime
    observations: list[Observation]
    warnings: list[str] = Field(default_factory=list)
    coverage_s: tuple[float, float] | None = Field(
        None, description="Time span (t_rel_s) the evidence covers; lets gaps in coverage be detected")


# ------------------------------------------------------- module 3: timeline

class TimelineEvent(BaseModel):
    id: str
    t_start_s: float
    t_end_s: float
    timestamp: datetime | None = None
    label: str
    category: Literal["vehicle_motion", "signal", "collision", "post_collision", "other"]
    vehicle_ids: list[str] = Field(default_factory=list)
    observation_ids: list[str]
    source_types: list[SourceType]
    confidence: float = Field(ge=0, le=1)
    uncertainty_s: float = Field(0.0, ge=0)
    basis: Literal["measured", "reported", "derived", "inferred"] | None = Field(
        None, description="Strongest basis among the supporting observations (measured > derived > reported > inferred)")
    basis_counts: dict[str, int] = Field(default_factory=dict)
    confidence_observed: float = Field(
        0.0, ge=0, le=1,
        description="What `confidence` would be on observations alone: the same combination, leaving out inferred "
                    "evidence (the AI model's readings). The gap to `confidence` is what rests on inference; 0 when "
                    "every supporting observation is an inference.")


class Timeline(BaseModel):
    schema_version: str = SCHEMA_VERSION
    incident_id: str
    reference_time: datetime
    generated_at: datetime
    events: list[TimelineEvent]


# ------------------------------------------------- module 4: contradictions

class ConflictType(str, Enum):
    numeric_mismatch = "numeric_mismatch"
    timestamp_overlap = "timestamp_overlap"
    semantic = "semantic"
    missing_evidence = "missing_evidence"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Conflict(BaseModel):
    id: str
    type: ConflictType
    severity: Severity
    title: str
    description: str
    observation_ids: list[str]
    t_range_s: tuple[float, float] | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    basis: Literal["rule", "llm"] = Field("rule", description="rule: deterministic check; llm: AI-detected, unverified")


class ConflictReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    incident_id: str
    generated_at: datetime
    conflicts: list[Conflict]


# ------------------------------------------------- module 5: reconstruction

class TrackPoint(BaseModel):
    t_s: float
    x_m: float = Field(description="Scene units: metres, or pixels when scene.units == 'px'")
    y_m: float
    heading_deg: float
    speed_kmh: float | None = None
    source: Literal["measured", "interpolated", "extrapolated", "tracked", "estimated"] | None = Field(
        None, description="measured: at a sensor sample; interpolated/extrapolated: between/beyond samples; "
                          "tracked: from video motion tracking; estimated: no positional evidence (scene default)")
    confidence: float | None = Field(None, ge=0, le=1)
    box: list[float] | None = Field(
        None, min_length=4, max_length=4,
        description="Detection box around the vehicle at this point, [x1, y1, x2, y2] in image pixels (footage plan, "
                    "scene.units == 'px' only). From the motion tracker's blob or the AI model's sighting; the box of an "
                    "AI-sighted vehicle (see VehicleTrack.description) is the model's reading, not a measurement.")


class VehicleTrack(BaseModel):
    vehicle_id: str
    label: str
    color: str
    description: str | None = None
    track: list[TrackPoint]
    source_observation_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list,
        description="What was added to the observation to get these positions (an assumed field of view, a road direction, "
                    "the AI model's reading). Empty when the positions are observed directly. Together with "
                    "source_observation_ids this links every inferred position to the evidence and the step beyond it.")


class CollisionMarker(BaseModel):
    t_s: float
    x_m: float
    y_m: float
    confidence: float = Field(ge=0, le=1)
    supporting_observation_ids: list[str]


class CameraImage(BaseModel):
    """The video frame a camera_image scene is drawn on (fetch it from /api/evidence/{id}/frames/{frame})."""
    evidence_id: str
    frame: int
    width: int
    height: int
    views: list[list[int]] = Field(default_factory=list, description="[x1, y1, x2, y2] per camera view, in px")
    fps: float | None = Field(None, description="Video frame rate: frame index = round((t_s - t_offset_s) * fps)")
    t_offset_s: float = 0.0


class Scene(BaseModel):
    """four_way_intersection: metres, +x east, +y north. camera_image: pixels of `image`, +y down.
    TrackPoint.x_m / y_m are in the scene's `units`."""
    type: Literal["four_way_intersection", "metric", "camera_image"] = "four_way_intersection"
    units: Literal["m", "px", "schematic"] = "m"
    extent_m: float | None = Field(None, description="Intersection: half-width of the square view around the origin")
    lane_width_m: float | None = None
    roads: dict[str, str] = Field(default_factory=dict, description="axis -> road name, e.g. {'ns': 'Main Rd'}")
    image: CameraImage | None = None
    center_m: tuple[float, float] | None = Field(None, description="metric: centre of the view (x, y) in metres")
    frame_note: str | None = Field(None, description="What the axes mean when not compass-aligned, e.g. a camera's road frame")


class Reconstruction(BaseModel):
    schema_version: str = SCHEMA_VERSION
    incident_id: str
    generated_at: datetime
    scene: Scene
    t_min_s: float
    t_max_s: float
    vehicles: list[VehicleTrack]
    collision: CollisionMarker | None = None
    note: str | None = Field(None, description="Why the reconstruction is empty or limited, in plain words")


# ------------------------------------------------------------ pipeline/API

class StageResult(BaseModel):
    stage: str
    status: Literal["real", "fixture", "skipped", "error"]
    message: str = ""
    duration_s: float | None = None


class EvidenceRef(BaseModel):
    """Which exact file a run used: the id plus its content hash."""
    id: str
    filename: str
    sha256: str | None = None


class AnalysisRun(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime
    evidence_count: int
    stages: list[StageResult]
    evidence: list[EvidenceRef] = Field(default_factory=list, description="Manifest of the evidence analysed")


class IncidentReport(BaseModel):
    """Investigator-facing report of one run (GET /api/report)."""
    run_id: str
    incident_id: str
    generated_at: datetime
    headline: str
    summary: str | None = Field(None, description="AI-drafted narrative, only when an LLM key is set")
    facts: dict[str, Any]
    markdown: str


class VehicleSpec(BaseModel):
    """A vehicle the investigator knows about. Evidence is linked to it by id, alias or description."""
    id: str = Field(min_length=1, max_length=40)
    description: str = Field("", max_length=200, description="e.g. 'red sedan, TS09 AB 1234'")
    aliases: list[str] = Field(default_factory=list, description="Other ids in the evidence, e.g. a telemetry unit id")
    ego: bool = Field(False, description="Carries the dashcam")


class SceneSpec(BaseModel):
    """What is known about the place. Nothing is assumed when type is 'unknown'."""
    type: Literal["unknown", "junction", "road"] = "unknown"
    traffic_side: Literal["left", "right"] | None = None
    roads: dict[str, str] = Field(default_factory=dict, description="junction: {'ns': name, 'ew': name}")
    lane_width_m: float | None = Field(None, gt=0, le=10)
    junction_center_m: tuple[float, float] = (0.0, 0.0)
    junction_radius_m: float | None = Field(None, gt=0, description="Box around the centre counted as 'in the junction'")


class LaneMarkings(BaseModel):
    """Lane-line standard used by automatic camera calibration (dash + gap = marking period)."""
    preset: str | None = None
    dash_m: float = Field(gt=0)
    gap_m: float = Field(gt=0)
    lane_width_m: float = Field(3.5, gt=0)


class IncidentInfo(BaseModel):
    """Case details (GET/PUT /api/incident). reference_time is t = 0 for every module."""
    incident_id: str
    title: str = ""
    location: str = ""
    reference_time: datetime
    reference_time_confirmed: bool = Field(description="False while the demo default is in use")
    vehicles: list[VehicleSpec] = Field(default_factory=list)
    scene: SceneSpec = Field(default_factory=SceneSpec)
    lane_markings: LaneMarkings | None = Field(None, description="None: no automatic camera calibration")


class IncidentUpdate(BaseModel):
    incident_id: str | None = Field(None, min_length=1, max_length=80)
    title: str | None = Field(None, max_length=200)
    location: str | None = Field(None, max_length=200)
    reference_time: datetime | None = Field(None, description="ISO 8601 with a UTC offset")
    vehicles: list[VehicleSpec] | None = None
    scene: SceneSpec | None = None
    lane_markings: LaneMarkings | str | None = Field(
        None, description="A LaneMarkings object or a preset name (see GET /api/incident/lane-presets)")


class CalibrationPoints(BaseModel):
    """Manual calibration of one camera view: >= 4 full-frame pixel points and their ground positions."""
    image_points: list[tuple[float, float]] = Field(min_length=4, description="[x_px, y_px] in the full video frame")
    ground_points: list[tuple[float, float]] = Field(min_length=4, description="[X_m, Y_m] on the road, same order")
    note: str = Field("", max_length=300, description="Where the ground positions came from")


class AuditEvent(BaseModel):
    """One line of the append-only audit log (storage/audit.jsonl)."""
    at: datetime
    event: str
    data: dict[str, Any] = Field(default_factory=dict)


class StageProgress(BaseModel):
    """Live state of one stage while a run is in progress."""
    stage: str
    status: Literal["pending", "running", "real", "fixture", "skipped", "error"] = "pending"
    done: int = 0
    total: int = 0
    note: str = ""


class AnalysisJob(BaseModel):
    """A background analysis run, polled by the dashboard (GET /api/analysis/jobs/current)."""
    job_id: str
    state: Literal["running", "done", "failed"]
    started_at: datetime
    finished_at: datetime | None = None
    stages: list[StageProgress]
    run: AnalysisRun | None = None
    error: str | None = None
