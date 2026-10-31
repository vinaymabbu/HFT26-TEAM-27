# Pitch pack: every placeholder in slides 2-8, filled from the code and real runs

Numbers come from `scripts/evaluate.py` (6 synthetic cases with answer keys) and runs of the real pipeline. Repo: https://github.com/vinaymabbu/HFT26-TEAM-27 (branch `feature/prototype`). There is no hosted demo: use a recorded screen capture, or run locally.

## Slide 2: Solution
- Pipeline: Video / Witness / Sensor -> Common Observation Schema -> Timeline + Contradiction engine -> Dashboard.
- Outputs: `timeline.json`, `conflicts.json`, positions JSON (`/api/reconstruction/positions`), also `observations.json`, `reconstruction.json`.
- "Never decides fault" is true in the code: the report prompt forbids causes/blame, and the UI and report say "not a legal finding".

## Slide 3: Extraction
- Video: FastAPI + OpenCV -> Featherless **Qwen/Qwen3-VL-30B-A3B-Instruct**. Scans at 4 fps to pick keyframes, sends at most **24 keyframes** (768 px JPEG) per video; fixed cameras also send a second frame 1 s later. Movement info: fixed camera = OpenCV motion tracks + AI boxes (position/direction, no km/h unless calibrated); dashcam = relative ego motion (moving / braking / stopped / turning), not km/h.
- Witness: Featherless **Qwen/Qwen3-30B-A3B-Instruct-2507** -> vehicle, action, order, time, hedges. Quotes must be verbatim; injected instructions are stripped.
- Sensor: Pandas. Layouts: vehicle track (timestamp/epoch + speed in km/h, mph, m/s or knots + x/y or lat/lon + heading + brake + vehicle id), signal log (time, approach, state), optical flow, frame index. Demo files: `signal_controller.csv` (timestamp,approach,state), `v1_telematics.csv` (timestamp,vehicle_id,speed_kmh,x_m,y_m,heading_deg,brake), `v2_obd.csv` (epoch_ms,unit_id,speed_mph,pos_x,pos_y,hdg,brake_pedal). Gap = step > 1.5x the median interval.
- Validation: max 200 MB; duplicates rejected by SHA-256 (409); video must decode; witness = non-empty UTF-8 .txt; CSV needs a recognised layout and a time column. Everything is Pydantic-validated JSON (schema 1.3.4).

## Slide 4: Timeline and contradictions
- Timeline: merge -> normalise to t = seconds from a reference -> sort -> cluster observations within 1.0 s + their uncertainties -> label.
- Checks: numeric (witness speed vs record: tolerated 15 km/h, high above 20 km/h), timestamp overlap (witness clock +-uncertainty vs record), semantic (rules for "no braking", "sped up", signal colour; optional LLM pass, labelled AI-detected), missing evidence (sensor dropouts, evidence ending before the collision, decisive events with one source). Cross-source time tolerance 1 s, high above 5 s.
- Real examples (junction demo): numeric "V2 speed overstated by W1" (at least 80 vs 57.6 km/h); semantic witness-vs-sensor "W1 says V2 no braking; telemetry shows braking"; missing evidence "V1 telemetry dropout 6-7.5 s". Video vs witness: dashcam case "witness says never slowed; dashcam shows braking". Witness vs witness: "V2 vehicle type ambiguous" (SUV vs "maybe a van"). Timestamp overlap: only demonstrated on a variant (W2 changed to "half past six": flagged high vs 17:42:10). The planted "about 17:45" is correctly NOT flagged (its +-15 min covers the record).
- No real "video gap" example exists; say "gaps" = sensor dropouts, single-source events, coverage.

## Slide 5: Traceability and uncertainty
- Observation vs inference: every item has a basis: measured / derived / reported / inferred (AI).
- Every observation has `evidence_ref` (e.g. `EVID-002, rows 14-15`); events, conflicts and path points list observation ids.
- Confidence: sensor 0.8-0.98; witness = 0.75 base, ceilings for hedges (maybe 0.5, I think 0.55, about 0.6), blended 0.6 lexical + 0.4 model certainty; video = model confidence; event = noisy-OR over **independent sources** (one file counts once, a fused observation is not added to its inputs). Path points: measured = sample confidence, interpolated decays exp(-gap/5), schematic 0.25.
- UI colours: >= 70% green, 40-70% amber, < 40% red. **Conflicts have a severity (low/medium/high) and cite observations, not a numeric confidence**: show "severity high, rule-based; cites obs X (conf 0.53), obs Y (0.98)".

## Slide 6: Dashboard
- Tabs: Overview, Evidence, Timeline, Contradictions, Reconstruction, Metrics, Footage. Timeline: search, source chips, "only open issues", click -> annotated frame + sources. Reconstruction: slider, play, jump to impact, observed (solid) vs inferred (dotted), inferred collision marker only when evidence supports it.
- Correct the bullet: positions are NOT predefined. Sensor data gives measured positions; video gives a labelled schematic (not to scale). Only the junction layout is declared per case.
- Screenshots: Reconstruction at t = 10 s (junction case); Timeline with an event open; Contradictions card.

## Slide 7: Demo and results
- Case: demo junction = 2 witness statements + 3 sensor logs (no video); real clips = 1 night crash clip, 1 four-view traffic clip. No single case has all three source types.
- Results (AI on): **8 of 8** expected contradictions found, **3 of 3** false-alarm checks passed, 8/8 events, 6/6 collisions; **1 planted gap** (V1 dropout) found. 42 checks total. Junction case passed 13/13 in 4 repeat runs.
- Honest misses (pick one): (a) we found and fixed two false alarms ourselves (a timezone bug flagged a correct witness; an invented speed) and added a false-alarm test; (b) the optional LLM pass once misread "had already turned yellow"; (c) night clip: collision dated 4.8 s vs about 5-6 s by eye, one vehicle tracked by motion.
- Data: junction scenario from `scripts/scenario.py` (ground truth) -> generated CSVs; witness statements written by us with 4 planted discrepancies; 6 synthetic evaluation cases (`scripts/make_cases.py`); real clips are third-party footage (the night clip carries a "Car Crash Time" watermark): test only, disclose it.

## Slide 8: Stack, limits, next
- React 19, Vite 8, Tailwind 4, SVG | FastAPI 0.162, Pydantic 2.14 | OpenCV 5.0, Pandas 3.1, NumPy 2.5 | Featherless.
- Limits: video positions are schematic without a lane-marking standard; model non-determinism; synthetic test data; not legal evidence. Next: metric trajectories from video, real CCTV/telematics, court-ready reports.
