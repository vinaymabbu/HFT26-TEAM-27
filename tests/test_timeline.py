"""Module 3: timeline built from the fixture observation sets, checked against scripts/scenario.py."""
import pytest

from app import store
from app.modules.timeline import build_timeline
from app.schemas import ObservationSet, SourceType, Timeline

import pytest as _pytest

pytestmark = _pytest.mark.usefixtures("demo_case")  # demo data: declared vehicles, junction, clock


@pytest.fixture
def obs_sets():
    return [store.load_fixture(f"{k}_observations.json", ObservationSet) for k in ("video", "witness", "sensor")]


@pytest.fixture
def timeline(obs_sets) -> Timeline:
    return build_timeline(obs_sets)


def _find(tl, category, pred=lambda e: True):
    return [e for e in tl.events if e.category == category and pred(e)]


def test_validates_and_sorted(timeline):
    Timeline.model_validate(timeline.model_dump())
    starts = [e.t_start_s for e in timeline.events]
    assert starts == sorted(starts)
    assert all(e.t_end_s >= e.t_start_s for e in timeline.events)
    assert len({e.id for e in timeline.events}) == len(timeline.events)


def test_every_observation_referenced_once(timeline, obs_sets):
    known = {o.id for s in obs_sets for o in s.observations}
    referenced = [i for e in timeline.events for i in e.observation_ids]
    assert set(referenced) <= known
    assert set(referenced) == known  # nothing dropped
    assert len(referenced) == len(set(referenced))


def test_collision_groups_all_sources(timeline):
    [col] = _find(timeline, "collision")
    assert col.t_start_s == pytest.approx(10.0, abs=0.2)
    assert set(col.vehicle_ids) == {"V1", "V2"}
    assert set(col.source_types) == {SourceType.video, SourceType.sensor, SourceType.witness}
    # wall-clock witness claims ("around 5:40", "quarter to six") describe the collision, not its time
    assert {"vid_005", "wit_w1_06", "wit_w1_01", "wit_w2_01"} <= set(col.observation_ids)


def test_signal_events(timeline):
    def sig(label):
        [e] = _find(timeline, "signal", lambda e: e.label == label)
        return e

    nb_yellow = sig("Northbound signal yellow")
    assert nb_yellow.t_start_s == pytest.approx(7.0)
    assert nb_yellow.t_end_s == pytest.approx(10.0)
    assert {"sig_001", "vid_002", "wit_w2_02"} <= set(nb_yellow.observation_ids)
    assert sig("Northbound signal red").t_start_s == pytest.approx(10.0)
    eb_red = sig("Eastbound signal red")
    assert (eb_red.t_start_s, eb_red.t_end_s) == (pytest.approx(-20.0), pytest.approx(11.0))
    assert sig("Eastbound signal green").t_start_s == pytest.approx(11.0)


def test_braking_and_entry(timeline):
    [v1_brake] = _find(timeline, "vehicle_motion", lambda e: e.vehicle_ids == ["V1"] and e.label.endswith("begins braking"))
    assert v1_brake.t_start_s == pytest.approx(9.0, abs=0.3)
    assert {"sen_v1_002", "vid_004", "wit_w1_05"} <= set(v1_brake.observation_ids)

    [v2_brake] = _find(timeline, "vehicle_motion", lambda e: e.vehicle_ids == ["V2"] and e.label.endswith("begins braking"))
    assert (v2_brake.t_start_s, v2_brake.t_end_s) == (pytest.approx(9.5), pytest.approx(10.0))

    [v2_entry] = _find(timeline, "vehicle_motion", lambda e: e.vehicle_ids == ["V2"] and "enters" in e.label)
    assert v2_entry.t_start_s == pytest.approx(9.1)
    assert "against red" in v2_entry.label


def test_motion_and_post_collision(timeline):
    [v1_move] = _find(timeline, "vehicle_motion", lambda e: e.label.startswith("V1 red sedan"))
    assert "47 km/h" in v1_move.label and v1_move.t_end_s == pytest.approx(9.0)
    [v2_move] = _find(timeline, "vehicle_motion", lambda e: e.label.startswith("V2 white SUV"))
    assert {"sen_v2_001", "wit_w1_03", "wit_w2_04"} <= set(v2_move.observation_ids)
    [rest] = _find(timeline, "post_collision")
    assert set(rest.vehicle_ids) == {"V1", "V2"} and rest.t_start_s == pytest.approx(11.5)


def test_derives_t_rel_from_timestamp(obs_sets):
    for s in obs_sets:
        for o in s.observations:
            o.t_rel_s = None
    tl = build_timeline(obs_sets)
    [col] = _find(tl, "collision")
    assert col.t_start_s == pytest.approx(10.0, abs=0.2)


def test_empty_input():
    assert build_timeline([]).events == []


def test_real_video_shapes_are_resolved(obs_sets):
    """Real module-1 output: free-text approach, other vehicle unnamed, collision names it by label."""
    video, witness, sensor = obs_sets
    by_id = {o.id: o for o in video.observations}
    by_id["vid_002"].attributes["approach"] = "ego lane"
    by_id["vid_003"].vehicle_id = None
    by_id["vid_003"].attributes.update(direction="from left (west)", label="car_2")
    by_id["vid_005"].attributes["vehicles"] = ["V1", "car_2"]
    # sensor bundle also reports the NB green phase starting long before the video
    sensor.observations.append(sensor.observations[-1].model_copy(update={
        "id": "sig_nb_green", "t_rel_s": -20.0,
        "attributes": {"approach": "NB", "signal_state": "green"}}))
    tl = build_timeline(obs_sets)

    [entry] = _find(tl, "vehicle_motion", lambda e: "vid_003" in e.observation_ids)
    assert entry.vehicle_ids == ["V2"]
    [yellow] = _find(tl, "signal", lambda e: "vid_002" in e.observation_ids)
    assert yellow.label == "Northbound signal yellow"
    [green] = _find(tl, "signal", lambda e: e.label == "Northbound signal green")
    assert green.t_start_s == pytest.approx(-20.0) and "vid_001" in green.observation_ids
    [col] = _find(tl, "collision")
    assert set(col.vehicle_ids) == {"V1", "V2"}
    # inputs untouched
    assert by_id["vid_002"].attributes["approach"] == "ego lane"


def test_several_provisional_vehicles_are_named_by_what_was_seen_not_by_id():
    from datetime import datetime, timezone
    from app.modules.timeline import build_timeline
    from app.schemas import Observation, ObservationSet, SourceRef

    def obs(i, action, t, vid=None, label=None, **attrs):
        return Observation(id=i, source_type="video", source_ref=SourceRef(evidence_id="ev_v", file="v.mp4", locator=f"frame:{int(t * 30)}"),
                           t_rel_s=t, time_uncertainty_s=0.3, action=action, vehicle_id=vid, vehicle_description=label, confidence=0.7,
                           attributes={"camera": "fixed", "view": 1, **({"label": label} if label else {}), **attrs})

    sets = [ObservationSet(source_type="video", evidence_id="ev_v", producer="t", generated_at=datetime.now(timezone.utc), observations=[
        obs("c", "collision", 4.8, vehicles=["white sedan"]),
        obs("s1", "stopped", 5.0, "U-B1-001", "white sedan"), obs("s2", "stopped", 5.2, "U-B1-002", "dark SUV")])]
    labels = [e.label for e in build_timeline(sets).events if e.category == "post_collision"]
    assert labels == ["white sedan and dark SUV (camera view 1) come to rest after the collision"], labels
    assert not any("U-B1" in label for label in labels)


# ----------------------------------------------------------------------- event confidence: independent sources only

def _obs(i, file, conf, derived=(), source="sensor"):
    from app.schemas import Observation, SourceRef
    return Observation(id=i, source_type=source, source_ref=SourceRef(evidence_id=file, file=f"{file}.csv", locator="row:2"),
                       t_rel_s=1.0, action="x", confidence=conf, derived_from=list(derived))


def test_one_observation_counts_once_however_often_it_is_listed():
    from app.modules.timeline import combined_confidence
    a = _obs("a", "f1", 0.85)
    assert combined_confidence([a]) == pytest.approx(0.85)
    assert combined_confidence([a, a, a]) == pytest.approx(0.85)


def test_repeated_observations_from_one_file_are_one_source_the_strongest():
    from app.modules.timeline import combined_confidence
    rows = [_obs(f"r{i}", "telematics", c) for i, c in enumerate([0.5, 0.98, 0.9])]
    assert combined_confidence(rows) == pytest.approx(0.98)


def test_agreement_between_independent_sources_raises_confidence():
    from app.modules.timeline import combined_confidence
    assert combined_confidence([_obs("a", "f1", 0.8), _obs("b", "f2", 0.7)]) == pytest.approx(1 - 0.2 * 0.3)
    assert combined_confidence([_obs("a", "f1", 0.8), _obs("b", "f2", 0.7)]) > 0.8     # corroboration helps, and only that


def test_an_observation_computed_from_others_is_not_counted_on_top_of_its_inputs():
    from app.modules.timeline import combined_confidence
    a, b = _obs("a", "v1", 0.9), _obs("b", "v2", 0.9)
    fused = _obs("f", "fusion", 0.81, derived=["a", "b"])
    assert combined_confidence([fused, a, b]) == pytest.approx(0.81)                   # the fused finding carries its inputs
    w = _obs("w", "stmt1", 0.79, source="witness")
    assert combined_confidence([fused, a, b, w]) == pytest.approx(1 - 0.19 * 0.21)     # an independent witness adds to it
    assert combined_confidence([a, b]) == pytest.approx(1 - 0.1 * 0.1)                 # without the fused one, the inputs stand


def test_a_witness_only_event_shows_the_witness_confidence_not_its_square(monkeypatch, tmp_path):
    """The bug: an ordered witness claim was counted as core evidence and again as an ordered claim, so 0.85 became 0.978."""
    from app.modules.extraction import extract_witness  # noqa: F401  (the mocked path below goes through it)
    from app.modules.timeline import build_timeline
    from test_extraction import _mock_witness
    text = "The red car sped up to beat the light.\n"
    reply = {"observations": [{"line": 1, "raw_text": "The red car sped up to beat the light", "vehicle_description": "red car",
                               "action": "accelerating", "order": 1, "certainty": "certain"}]}
    out, _ = _mock_witness(monkeypatch, tmp_path, text, [reply])
    (claim,) = out.observations
    (event,) = [e for e in build_timeline([out]).events if claim.id in e.observation_ids]
    assert event.confidence == pytest.approx(claim.confidence, abs=0.001)


def test_event_confidence_is_split_into_what_rests_on_observation_and_what_rests_on_inference():
    """An event's confidence_observed is the same combination without the inferred (AI-read) evidence."""
    from datetime import datetime, timezone
    from app.modules.timeline import build_timeline
    from app.schemas import Observation, ObservationSet, SourceRef

    def make(i, source, file, conf, basis):
        return Observation(id=i, source_type=source, source_ref=SourceRef(evidence_id=file, file=f"{file}.dat", locator="row:2"),
                           t_rel_s=4.0, time_uncertainty_s=0.3, action="collision", vehicle_description="white sedan", confidence=conf,
                           basis=basis, attributes={})

    def event(*obs):
        sets = [ObservationSet(source_type=o.source_type, evidence_id=o.source_ref.evidence_id, producer="t",
                               generated_at=datetime.now(timezone.utc), observations=[o]) for o in obs]
        (e,) = [e for e in build_timeline(sets).events if e.category == "collision"]
        return e

    sensor, ai = make("s", "sensor", "f_sensor", 0.9, "measured"), make("a", "video", "f_video", 0.6, "inferred")
    both = event(sensor, ai)
    assert both.confidence == pytest.approx(1 - 0.1 * 0.4, abs=0.001)           # corroboration raises the total
    assert both.confidence_observed == pytest.approx(0.9, abs=0.001)           # observation alone: the sensor
    assert both.confidence_observed < both.confidence                          # the gap is what rests on inference
    only_ai = event(ai)
    assert only_ai.confidence == pytest.approx(0.6, abs=0.001) and only_ai.confidence_observed == 0.0
    only_sensor = event(sensor)
    assert only_sensor.confidence_observed == only_sensor.confidence
