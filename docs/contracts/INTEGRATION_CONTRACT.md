# Integration contract

**Source of truth:** [`src/backend/app/schemas.py`](../../src/backend/app/schemas.py) (schema version `1.3.6`).
JSON Schema copies live in [`schemas/`](schemas/). Regenerate them with `python scripts/export_schemas.py`.
Example payloads for every shape are in [`/fixtures`](../../fixtures).

## Data flow

```
upload ──► Evidence ──► [1] analyze_video    ─┐
                    ──► [2] extract_witness  ─┼─► list[ObservationSet]
                    ──► [2] extract_sensor   ─┘          │
                                                         ▼
                                     [3] build_timeline ──► Timeline
                                                         │
                         [4] detect_conflicts(obs, timeline) ──► ConflictReport
                                                         │
       [5] build_reconstruction(obs, timeline, conflicts) ──► Reconstruction
                                                         │
                                             [6] Dashboard (React) via /api
```

## Case settings: scenario knowledge lives here, never in code

`PUT /api/incident` declares what the investigator knows. With nothing declared, **nothing is assumed**:
no junction, no vehicle names, no lane-marking standard, no clock.

| Setting | Effect |
| --- | --- |
| `reference_time` | t = 0. If unset, the earliest absolute timestamp in the sensor evidence is used (the report says so). With neither, clock times are not shown |
| `vehicles[{id, description, aliases, ego}]` | Telemetry units (aliases) and witness/video descriptions are linked to these ids. `ego: true` marks the dashcam car |
| `scene{type: unknown\|junction\|road, traffic_side, roads, lane_width_m, junction_center_m, junction_radius_m}` | Only a declared junction gets "enters the junction" events, a drawn crossroads and lane-default trajectories |
| `reference_time: null` | An explicit null clears a previously declared clock (switching case): t = 0 comes from the evidence again. Leaving the field out leaves it unchanged |
| `lane_markings` (`de_motorway`, `de_urban`, `us_freeway` or `{dash_m, gap_m, lane_width_m}`) | Required for automatic camera calibration; without it, no km/h from video |

## Observation vs inference (`basis`)

Every observation has `basis`: **measured** (read from a sensor or log), **reported** (a witness), **derived**
(computed by deterministic rules: tracking, calibration, fusion, thresholds) or **inferred** (an AI model's
reading). Derived observations list `derived_from`. Timeline events carry the strongest basis behind them and
`basis_counts`, and `confidence_observed` (the event's confidence on observations alone, leaving out inferred ones; the gap to `confidence` is what rests on inference; added in 1.3.6); conflicts carry `basis: rule | llm`; track points carry `source` (measured / interpolated /
extrapolated / tracked / estimated) and `confidence`.

## Shared conventions

| Concept | Rule |
| --- | --- |
| Time | `t_rel_s` = seconds from the incident reference time (`Timeline.reference_time`). Absolute times are ISO-8601 with an offset (the demo uses IST, `+05:30`). Unknown → `null`, never a guess. |
| Uncertainty | `time_uncertainty_s` / `uncertainty_s` = ± seconds. `confidence` is 0–1. |
| Space | Metres, +x east, +y north (GPS is converted to local metres around one origin per run; a declared junction is centred at its `junction_center_m`). `heading_deg`: 0 = north, clockwise. Camera scenes are in image pixels. |
| Vehicle ids | Declared case ids (or a telemetry unit's own id). Undeclared descriptions get a provisional `U-<colour>-<type>` shared across sources; unresolvable ones stay `null` with the text in `vehicle_description`. Links are recorded in `attributes.vehicle_link`. |
| Speed | Always `attributes.speed_kmh`. Keep the raw value in `speed_<unit>_raw` if you converted. |
| Source refs | Every observation has `source_ref.locator`: `frame:<n>` (video), `line:<n>` (witness), `row:<n>` / `rows:<a>-<b>` (CSV, 1-based incl. header). |
| Ids | Observations `vid_*`, `wit_*`, `sen_*`, `sig_*`; events `evt_*`; conflicts `cf_*`. Unique within a run. |
| Actions | Free string, but prefer: `moving`, `braking`, `accelerating`, `no_braking`, `entering_intersection`, `collision`, `post_collision_slide`, `stopped`, `signal_state`, `sudden_heading_change`, `sensor_gap`. Add new ones to this list. |

## Shapes (summary — see schemas.py for every field)

- **Observation** — `id, source_type, source_ref{evidence_id,file,locator}, timestamp, t_rel_s, time_uncertainty_s, vehicle_id, vehicle_description, action, attributes{}, confidence, raw_text`
- **ObservationSet** — one extractor run on one Evidence: `source_type, evidence_id, producer, generated_at, observations[], warnings[]`
- **Timeline** — `incident_id, reference_time, events[]`; each **TimelineEvent** has `t_start_s, t_end_s, label, category, vehicle_ids, observation_ids, source_types, confidence, uncertainty_s`
- **ConflictReport** — `conflicts[]`; each **Conflict** has `type ∈ {numeric_mismatch, timestamp_overlap, semantic, missing_evidence}`, `severity ∈ {low, medium, high}`, `title, description, observation_ids, t_range_s, details{}`
- **Evidence** has `ref` (`EVID-002`); each **Observation** has `evidence_ref`, a readable pointer into the original (`EVID-002, line 3`, `EVID-003, rows 412-415`, `EVID-001, frame 375`), set at the end of every run. `source_ref` keeps the machine form (`evidence_id`, `file`, `locator`)
- **Reconstruction** — `scene{extent_m, lane_width_m, roads}, t_min_s, t_max_s, vehicles[{vehicle_id, label, color, track[{t_s,x_m,y_m,heading_deg,speed_kmh}]}], collision{t_s,x_m,y_m,confidence,supporting_observation_ids}`
  - `scene.type = "four_way_intersection"` (units m, +y north) from sensor tracks, or `"camera_image"` (units **px**, +y down) from fixed-camera video: `scene.image{evidence_id, frame, width, height, views, fps, t_offset_s}` is the frame the tracks are drawn on (`/api/evidence/{id}/frames/{n}`). Track ids `T<view>-<n>` are moving vehicles, `S<view>-<n>` are verified stopped vehicles. Metric tracks win when both exist
  - `scene.units = "schematic"` (type `metric`): layout and order of movement only. Image positions are mapped onto a flat road with a *nominal* camera (horizon just above the view, ~60 degree field of view), so units are not metres. Chosen over an empty panel when a fixed camera has tracks but no calibration; the dashboard shows it with a warning banner
  - Fixed camera, one view, fewer than 3 motion tracks: module 1 also links the vision model's vehicle boxes into coarse tracks (`track_id` `B<view>-<n>`, `attributes.source`, basis `inferred`, confidence 0.35, position = box bottom centre), skipping any that duplicate a motion track. A `collision` observation carries `attributes.timing` ("at or before") and a `time_uncertainty_s` of at least the gap to the previous keyframe; one raised from a vehicle description has `attributes.reported_in`, `box`, `label` Linking is by position, size, colour and a soft label with the road direction as a prior (`ingestion/linking.py`); each track carries `labels` (every name the model used, with counts) and `sightings`. A vehicle that moved and then stayed at one place across at least 3 keyframes over at least 1.5 s also gives a `stopped` observation (`verified_by`, `seen_in_keyframes`, `first_seen_s`, `last_seen_s`, `box`, `vehicle_id` `U-B1-n` = its track), unless it never moved (parked) or the pixel check already confirmed the stop; pedestrians are not tracked
  - Fixed-camera track observations may carry `attributes.vanishing_point_px` (full-frame pixels; where the road's lane lines, rails and kerbs meet) and `attributes.road_geometry` (segments, inliers, distinct lines, spread, traffic check). It gives the road's *direction* only, not a calibration, and is set only when the picture and the traffic support it. The schematic plan uses it to draw a road-aligned plan (lanes parallel, road up the page; `scene.frame_note` and `note` say so, the camera's field of view is still assumed); without it the plan uses a nominal camera
  - Module 1 delivers camera tracks as observations with `action: "track"` and `attributes.points = [[t, x_px, y_px], …]`. The timeline and contradiction modules ignore them

## HTTP API (FastAPI, port 8000)

| Method | Path | Returns |
| --- | --- | --- |
| GET | `/api/health` | status, schema version |
| POST | `/api/evidence/video`, `/api/evidence/witness`, `/api/evidence/sensor` | Same as the generic upload, but the endpoint fixes the expected kind (**415** if the file is another kind). Witness: readable UTF-8 `.txt`, not empty. Sensor: `.csv` with a recognised layout and a time column; `details.columns_found` says which of `timestamp` / `speed` it has and `details.upload_warnings` flags a missing speed column (accepted: signal logs and position-only GPS are valid). Every accepted file gets a case-wide display id `ref` (`EVID-001`, `EVID-002`, … in upload order, never reused after a delete) and the original is stored unchanged with its sha256 |
| GET / POST | `/api/evidence` | list / upload (`multipart file`). Type by extension: `.mp4 .mov .avi .mkv` → video, `.txt` → witness, `.csv` → sensor. Upload records `sha256` and `details` (video fps/duration, CSV layout); **409** for a duplicate, **422** for an unusable file (undecodable video, unknown CSV layout, empty/non-UTF-8 text) |
| DELETE | `/api/evidence/{id}` | 204 |
| POST | `/api/analysis/run` | Synchronous run → `AnalysisRun`, per-stage status `real \| fixture \| skipped \| error`. 409 while another run is in progress |
| POST | `/api/analysis/jobs` | Starts a background run (or returns the running one) → `AnalysisJob` (202) |
| GET | `/api/analysis/jobs/current` | Latest `AnalysisJob` with live per-stage progress (`pending \| running` + `done/total`), or `null` |
| GET | `/api/evidence/{id}/frames/{index}?width=480` | One frame of an uploaded video as JPEG (thumbnails for `frame:<n>` refs) |
| GET | `/api/evidence/{id}/calibration` | Per camera view: `manual` and `auto` (lane-dash) calibrations, with the reason a view is or isn't calibrated |
| PUT / DELETE | `/api/evidence/{id}/calibration/{view}` | Manual calibration `{image_points: [[x_px, y_px] ×≥4], ground_points: [[X_m, Y_m] ×≥4], note}` → homography (422 if collinear or fewer than 4 points). Overrides the automatic one on the next run; audited |
| GET / PUT | `/api/incident` | Case details `IncidentInfo`: id, title, location, **reference time** (t = 0). Re-run after changing it |
| GET | `/api/report?format=json\|markdown&narrative=true` | `IncidentReport` of the latest run (Markdown document with citations; AI summary only with a key) |
| GET | `/api/runs` | Every kept run (last 50), newest first, each with its evidence manifest (sha256) and stage timings |
| GET | `/api/runs/{run_id}/{run\|observations\|timeline\|conflicts\|reconstruction\|report}` | Outputs of a past run |
| GET | `/api/audit?limit=200` | Append-only audit log: uploads, rejections, deletions, incident changes, runs |
| GET | `/api/analysis/latest` | last `AnalysisRun` or `null` |
| GET | `/api/observations` | `ObservationSet[]` |
| GET | `/api/timeline` | `Timeline` |
| GET | `/api/conflicts` | `ConflictReport` |
| GET | `/api/reconstruction` | `Reconstruction`: the **top-down map in metres** (sensor/GPS paths, else a calibrated camera view on the road plane, else a **schematic** plan from an uncalibrated fixed camera (`scene.units: "schematic"`, every point `estimated`, no speeds, no collision marker, `note` states the assumptions), else empty with `note` saying what evidence is missing) |
| GET | `/api/reconstruction/positions?download=false` | The same reconstruction as a plain "id and positions" JSON: `vehicles[{id, label, positions[{t_s, x, y, heading_deg, speed_kmh, source, basis, observation_ids, confidence}]}]` (`source` = measured / interpolated / extrapolated / tracked / estimated; `basis` = `sensor` \| `video` \| `simulated`, where simulated is bundled demo data or a position with no evidence behind it; `observation_ids` = the observations the point rests on: its own sample, the two neighbouring samples when filled in, plus the sensor-gap observation that explains a filled-in stretch), `collision{t_s, x, y, confidence}`, and a `coordinate_system` block (metres +x east / +y north, or camera pixels). Each vehicle also lists `assumptions` (what was added to the observation to get its positions: an assumed field of view, a road direction, the AI model's reading; empty when the positions are observed directly) next to `source_observation_ids` (added in 1.3.6). `download=true` sends it as `vehicle_positions.json` |
| GET | `/api/event-analysis` | `Reconstruction` with a `camera_image` scene: footage with detections and image-space tracks (pixels), or `null` without video tracks |

Before the first run, the GET endpoints serve fixtures.

## Changing the contract

1. Additive changes (a new optional field, a new action string) are fine. Bump the patch version and update the fixtures.
2. Renaming or removing a field, or changing a type, is a breaking change. Agree on it with the team, bump the minor version, and update `schemas.py`, the fixtures, `scripts/export_schemas.py` output and the frontend in **one** commit.
3. `pytest` must pass (`tests/test_fixtures.py` validates every fixture against the models).
