"""Module 3 — Timeline reconstruction.

Collect observations, normalise timestamps, sort, correlate observations of
the same event, group, emit timeline.json.

Pure rules (Pandas/NumPy), no LLM. Observations fall into three timing classes:

- anchored: `t_rel_s`, or `timestamp` minus the reference time, with a usable
  `time_uncertainty_s`. These are clustered into events.
- weak: a time is given but its uncertainty is huge (a witness saying "around
  5:40"). The claim describes an event, not a time, so it joins the nearest
  matching anchored event without moving it.
- ordered: no time at all, only `attributes.order` within one statement. Each
  is matched, in statement order, to an anchored event of the same kind; ones
  with no match are placed between their placed neighbours.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import re

import numpy as np
import pandas as pd

from app import config
from app.schemas import BASIS_RANK, Observation, ObservationSet, SourceType, Timeline, TimelineEvent

def _ref() -> datetime:
    """The incident reference time (t = 0), read at call time: it is a per-case setting."""
    return config.INCIDENT_REFERENCE_TIME

BASE_TOLERANCE_S = 1.0     # two anchored observations this close (plus their uncertainties) can be one event
WEAK_UNCERTAINTY_S = 30.0  # above this a timestamp is a rough clock claim, not a time

APPROACH_NAMES = {"NB": "Northbound", "SB": "Southbound", "EB": "Eastbound", "WB": "Westbound"}
COMPASS = {"north": "NB", "south": "SB", "east": "EB", "west": "WB"}
OPPOSITE = {"NB": "SB", "SB": "NB", "EB": "WB", "WB": "EB"}
APPROACH_FROM = {"NB": "south", "SB": "north", "EB": "west", "WB": "east"}

# action -> family. Unknown actions are their own family.
FAMILIES = {
    "moving": "moving", "cruising": "moving", "driving": "moving",
    "braking": "braking", "decelerating": "braking",
    "entering_intersection": "entering",
    "collision": "collision", "impact": "collision",
    "stopped": "stopped", "post_collision_slide": "stopped",
    "sudden_heading_change": "heading_change",
    "signal_state": "signal",
}
# Families that refer to the whole incident rather than one vehicle.
SHARED_FAMILIES = {"collision", "post_collision"}
VEHICLE_WORDS = {"sedan", "suv", "car", "van", "truck", "lorry", "bus", "bike", "motorcycle", "scooter",
                 "hatchback", "jeep", "auto", "rickshaw", "pickup", "taxi"}
VERBS = {"no_braking": "does not brake", "accelerating": "accelerates", "turning": "turns",
         "crossing_intersection": "crosses the junction"}
COLOUR_WORDS = {"red", "white", "black", "silver", "grey", "gray", "blue", "green", "yellow", "orange", "brown", "maroon"}
CATEGORY_ORDER = {"signal": 0, "vehicle_motion": 1, "collision": 2, "post_collision": 3, "other": 4}


# ------------------------------------------------------------------ helpers

def _vehicles(o: Observation) -> set[str]:
    vs = set(o.attributes.get("vehicles") or [])
    if o.vehicle_id:
        vs.add(o.vehicle_id)
    return vs


def _t_rel(o: Observation) -> float | None:
    if o.t_rel_s is not None:
        return float(o.t_rel_s)
    if o.timestamp is not None:
        ts = o.timestamp if o.timestamp.tzinfo else o.timestamp.replace(tzinfo=_ref().tzinfo)
        return (ts - _ref()).total_seconds()
    return None


def _statement_key(o: Observation) -> str:
    return f"{o.source_ref.evidence_id}|{o.source_ref.file}"


def combined_confidence(observations) -> float:
    """How well the evidence supports an event: agreement between *independent* sources raises it, the same evidence is
    never counted twice.

    * every observation counts once (an observation can sit in a cluster as core evidence and as an ordered claim);
    * observations from one file or statement are one source, represented by the strongest: a telematics log with 25 rows
      or a witness who says the same thing three times is not three confirmations;
    * an observation computed from others (a fused collision from two heading changes) already carries them, so its
      inputs are not added on top;
    * sources are then combined by noisy-OR: 1 - prod(1 - c), so each independent source can only add confidence.
    """
    unique = list({o.id: o for o in observations}.values())
    consumed = {d for o in unique for d in o.derived_from}
    best: dict[str, float] = {}
    for o in unique:
        if o.id in consumed:
            continue
        key = o.source_ref.evidence_id
        best[key] = max(best.get(key, 0.0), o.confidence)
    return _noisy_or(best.values())


def _noisy_or(confs) -> float:
    confs = np.clip(np.asarray(list(confs), dtype=float), 0.0, 1.0)
    if confs.size == 0:
        return 0.0
    return float(1.0 - np.prod(1.0 - confs))


def _heading_to_approach(heading: float) -> str:
    return ["NB", "EB", "SB", "WB"][int(((heading % 360) + 45) // 90) % 4]


def _parse_direction(text) -> str | None:
    """'eastbound' / 'heading east' -> EB; 'from the west' / 'enters from left (west)' -> EB; 'NB' -> NB."""
    if not isinstance(text, str):
        return None
    if text.strip().upper() in APPROACH_NAMES:
        return text.strip().upper()
    low = text.lower()
    m = re.search(r"from (?:the )?(?:\w+ )?\(?(north|south|east|west)", low)
    if m:
        return OPPOSITE[COMPASS[m.group(1)]]
    m = re.search(r"(north|south|east|west)", low)
    return COMPASS[m.group(1)] if m else None


def _approach_of(obs: list[Observation]) -> str | None:
    for o in obs:
        a = o.attributes
        if (d := _parse_direction(a.get("direction"))) is not None:
            return d
        if a.get("heading_deg") is not None:
            return _heading_to_approach(float(a["heading_deg"]))
    return None


def _desc_words(desc: str | None) -> set[str]:
    return set(re.findall(r"[a-z]+", (desc or "").lower())) & (VEHICLE_WORDS | COLOUR_WORDS)


def _resolve(observations: list[Observation]) -> list[Observation]:
    """Copies of the observations with vehicle ids and signal approaches filled in where they can be inferred.

    Video reports other vehicles by description/direction only and signals by free-text approach
    ("ego lane"); match those against vehicles whose ids are known. Inputs are never mutated.
    """
    profiles: dict[str, dict[str, set]] = {}
    ego = None
    for o in observations:
        if not o.vehicle_id:
            continue
        p = profiles.setdefault(o.vehicle_id, {"approach": set(), "words": set()})
        p["words"] |= _desc_words(o.vehicle_description)
        if o.action != "signal_state" and (a := _approach_of([o])):
            p["approach"].add(a)
        if o.attributes.get("ego"):
            ego = o.vehicle_id

    def match(o: Observation) -> str | None:
        approach, words = _approach_of([o]), _desc_words(o.vehicle_description)
        if not approach and not words:
            return None
        scores = {}
        for vid, p in profiles.items():
            if approach and p["approach"] and approach not in p["approach"]:
                continue
            scores[vid] = 2 * bool(approach and approach in p["approach"]) + len(words & p["words"])
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if ranked and ranked[0][1] > 0 and (len(ranked) == 1 or ranked[1][1] < ranked[0][1]):
            return ranked[0][0]
        return None

    resolved, labels = [], {}
    for o in observations:
        update: dict = {}
        attrs = dict(o.attributes)
        if o.action == "signal_state":
            raw = attrs.get("approach")
            ego_approach = next(iter(profiles.get(ego or "V1", {}).get("approach", [])), None)
            appr = _parse_direction(raw) or (ego_approach if isinstance(raw, str) and "ego" in raw.lower() else None)
            if appr:
                attrs["approach"] = appr
        elif not o.vehicle_id and not attrs.get("vehicles") and (vid := match(o)):
            update["vehicle_id"] = vid
            if attrs.get("label"):
                labels[attrs["label"]] = vid
        update["attributes"] = attrs
        resolved.append(o.model_copy(update=update))
    for o in resolved:  # collision lists may name vehicles by video label
        if o.attributes.get("vehicles"):
            o.attributes["vehicles"] = [labels.get(v, v) for v in o.attributes["vehicles"]]
    return resolved


@dataclass
class _Cluster:
    family: str
    key: tuple
    vehicles: set[str] = field(default_factory=set)
    anchored: list[tuple[Observation, float]] = field(default_factory=list)
    attached: list[Observation] = field(default_factory=list)  # weak + ordered members
    placed_range: tuple[float, float] | None = None             # for clusters made only of ordered obs
    placed_uncertainty: float = 0.0

    @property
    def members(self) -> list[Observation]:
        return [o for o, _ in self.anchored] + self.attached

    def core_times(self) -> np.ndarray:
        primary = [t for o, t in self.anchored if FAMILIES.get(o.action, o.action) == self.family]
        return np.asarray(primary or [t for _, t in self.anchored], dtype=float)

    @property
    def t(self) -> float:
        if self.anchored:
            return float(self.core_times().min())
        return self.placed_range[0]

    @property
    def uncertainty(self) -> float:
        if not self.anchored:
            return self.placed_uncertainty
        uncs = [o.time_uncertainty_s for o, _ in self.anchored]
        if self.family == "signal":  # later sightings of a phase say nothing about its onset
            first = min(t for _, t in self.anchored)
            return float(min(o.time_uncertainty_s for o, t in self.anchored if t - first <= BASE_TOLERANCE_S))
        return float(max(min(uncs), np.std(self.core_times())))

    def time_range(self) -> tuple[float, float]:
        if not self.anchored:
            return self.placed_range
        times = list(self.core_times())
        for o, _ in self.anchored:  # an explicit onset interval widens the event
            onset = o.attributes.get("brake_onset_between_s") or o.attributes.get("onset_between_s")
            if isinstance(onset, (list, tuple)) and len(onset) == 2:
                times += [float(onset[0]), float(onset[1])]
            if o.action == "sensor_gap" and "gap_start_s" in o.attributes:
                times += [float(o.attributes["gap_start_s"]), float(o.attributes.get("gap_end_s", o.attributes["gap_start_s"]))]
        return float(min(times)), float(max(times))


# --------------------------------------------------------------- the module

class _Builder:
    def __init__(self, observation_sets: list[ObservationSet]):
        rows = [{"obs": o, "id": o.id, "t": _t_rel(o), "unc": float(o.time_uncertainty_s),
                 "order": o.attributes.get("order"), "stmt": _statement_key(o)}
                for o in _resolve([o for s in observation_sets for o in s.observations
                                   if o.action != "track"])]  # raw motion tracks belong to the reconstruction
        df = pd.DataFrame(rows, columns=["obs", "id", "t", "unc", "order", "stmt"])
        df = df.drop_duplicates(subset="id", keep="first")
        timed = df["t"].notna()
        weak = timed & (df["unc"] > WEAK_UNCERTAINTY_S)
        self.anchored = df[timed & ~weak].sort_values(["t", "unc"], kind="stable")
        self.weak = df[weak]
        self.untimed = df[~timed]
        self.clusters: list[_Cluster] = []
        self.collision_t = self._collision_time()

    def _collision_time(self) -> float | None:
        col = self.anchored[self.anchored["obs"].map(lambda o: FAMILIES.get(o.action) == "collision")]
        if col.empty:
            return None
        w = 1.0 / (col["unc"].to_numpy() + 0.05) ** 2
        return float(np.average(col["t"].to_numpy(dtype=float), weights=w))

    def _family(self, o: Observation, t: float | None) -> str:
        fam = FAMILIES.get(o.action, o.action)
        ct = self.collision_t
        if fam == "heading_change" and ct is not None and t is not None:
            if abs(t - ct) <= BASE_TOLERANCE_S + o.time_uncertainty_s:
                return "collision"  # an abrupt yaw at impact time is the impact signature
            if t > ct:
                return "post_collision"
        if fam == "stopped" and (o.action == "post_collision_slide" or (ct is not None and (t is None or t >= ct))):
            return "post_collision"
        return fam

    def _key(self, o: Observation, fam: str) -> tuple:
        if fam == "signal":
            return ("signal", o.attributes.get("approach"), o.attributes.get("signal_state"))
        return (fam,)

    def _compatible(self, c: _Cluster, o: Observation, fam: str) -> bool:
        if c.family != fam or c.key != self._key(o, fam):
            return False
        if fam in SHARED_FAMILIES or fam == "signal":
            return True
        vs = _vehicles(o)
        return not vs or not c.vehicles or bool(vs & c.vehicles)

    def _candidates(self, o: Observation, fam: str) -> list[_Cluster]:
        return [c for c in self.clusters if c.anchored and self._compatible(c, o, fam)]

    # 1. cluster anchored observations, in time order
    def cluster_anchored(self):
        phase: dict[str, _Cluster] = {}  # approach -> current signal phase
        for row in self.anchored.itertuples():
            o, t = row.obs, float(row.t)
            fam = self._family(o, t)
            if fam == "signal":
                # a state holds until the approach changes state, so later sightings of it join the phase
                key = self._key(o, fam)
                cur = phase.get(key[1])
                if cur is None or cur.key != key:
                    cur = phase[key[1]] = _Cluster(family=fam, key=key)
                    self.clusters.append(cur)
                cur.anchored.append((o, t))
                continue
            best, best_dt = None, None
            for c in self._candidates(o, fam):
                ref_t = float(np.mean([ct for _, ct in c.anchored]))
                tol = BASE_TOLERANCE_S + o.time_uncertainty_s + max(oo.time_uncertainty_s for oo, _ in c.anchored)
                dt = abs(t - ref_t)
                if dt <= tol and (best_dt is None or dt < best_dt):
                    best, best_dt = c, dt
            if best is None:
                best = _Cluster(family=fam, key=self._key(o, fam))
                self.clusters.append(best)
            best.anchored.append((o, t))
            best.vehicles |= _vehicles(o)

    # 2. weak clock claims join the nearest matching event
    def attach_weak(self):
        for row in self.weak.itertuples():
            o, t = row.obs, float(row.t)
            fam = self._family(o, t)
            cands = self._candidates(o, fam)
            if cands:
                min(cands, key=lambda c: abs(c.t - t)).attached.append(o)
            else:
                c = _Cluster(family=fam, key=self._key(o, fam), vehicles=_vehicles(o),
                             attached=[o], placed_range=(t, t), placed_uncertainty=row.unc)
                self.clusters.append(c)

    # 3. ordered (untimed) statements, one statement at a time
    def place_untimed(self):
        for _, stmt in self.untimed.groupby("stmt", sort=False):
            ordered = stmt[stmt["order"].notna()].sort_values("order", kind="stable")
            unordered = stmt[stmt["order"].isna()]
            placed: list[tuple[Observation, _Cluster | None, float | None]] = []
            last_t = -np.inf
            for o in ordered["obs"]:
                fam = self._family(o, None)
                cands = sorted(self._candidates(o, fam), key=lambda c: c.t)
                after = [c for c in cands if c.t >= last_t - BASE_TOLERANCE_S]
                chosen = after[0] if after else (cands[-1] if cands else None)
                if chosen is not None:
                    chosen.attached.append(o)
                    last_t = max(last_t, chosen.t)
                placed.append((o, chosen, chosen.t if chosen else None))
            self._place_gaps(placed)

            stmt_times = [t for _, _, t in placed if t is not None]
            for o in unordered["obs"]:
                fam = self._family(o, None)
                cands = sorted(self._candidates(o, fam), key=lambda c: c.t)
                if cands:
                    cands[0].attached.append(o)
                elif stmt_times:
                    t0 = float(np.median(stmt_times))
                    spread = float(np.ptp(stmt_times)) if len(stmt_times) > 1 else WEAK_UNCERTAINTY_S
                    self._new_placed(o, fam, t0, t0, spread / 2)
                else:  # same: sometime within the anchored evidence
                    lo, hi = self._span()
                    self._new_placed(o, fam, lo, hi, max((hi - lo) / 2, WEAK_UNCERTAINTY_S / 2))

    def _span(self) -> tuple[float, float]:
        """Time span of the anchored evidence: where an unplaceable claim could have happened."""
        times = [c.t for c in self.clusters if c.anchored]
        return (min(times), max(times)) if times else (0.0, 0.0)

    def _place_gaps(self, placed):
        """Unmatched ordered observations span the gap between their placed neighbours."""
        times = [t for _, _, t in placed]
        for i, (o, chosen, t) in enumerate(placed):
            if chosen is not None:
                continue
            prev = next((times[j] for j in range(i - 1, -1, -1) if times[j] is not None), None)
            nxt = next((times[j] for j in range(i + 1, len(times)) if times[j] is not None), None)
            lo = prev if prev is not None else nxt
            hi = nxt if nxt is not None else prev
            if lo is None:  # nothing to anchor to: it happened sometime within the evidence, not at t = 0
                lo, hi = self._span()
            lo, hi = min(lo, hi), max(lo, hi)
            self._new_placed(o, self._family(o, None), lo, hi, (hi - lo) / 2)

    def _new_placed(self, o, fam, lo, hi, unc):
        if fam == "post_collision" and self.collision_t is not None and lo < self.collision_t:
            # "came to rest after the collision" cannot have happened before it
            lo = self.collision_t
            hi = max(hi, lo)
            unc = max(unc, (hi - lo) / 2)
        self.clusters.append(_Cluster(family=fam, key=self._key(o, fam), vehicles=_vehicles(o),
                                      attached=[o], placed_range=(lo, hi), placed_uncertainty=unc))

    # 4. turn clusters into events
    def events(self) -> list[TimelineEvent]:
        ranges = {id(c): list(c.time_range()) for c in self.clusters}
        self._extend_states(ranges)
        descs = self._vehicle_descriptions()

        drafts = []
        for c in self.clusters:
            members = c.members
            core = [o for o, _ in c.anchored] or c.attached
            lo, hi = ranges[id(c)]
            support = core + [a for a in c.attached if a.attributes.get("order") is not None]
            drafts.append({
                "t_start_s": round(lo, 3), "t_end_s": round(max(hi, lo), 3),
                "label": self._label(c, descs, lo),
                "category": self._category(c),
                "vehicle_ids": sorted(c.vehicles),
                "observation_ids": [o.id for o in members],
                "source_types": sorted({o.source_type for o in members}, key=lambda s: s.value),
                # clock claims say what happened, not when: they add no confidence
                "confidence": round(combined_confidence(support), 3),
                # the same, resting on observation alone (measured, derived or reported), not on the model's readings
                "confidence_observed": round(combined_confidence([o for o in support if o.basis != "inferred"]), 3),
                "uncertainty_s": round(c.uncertainty, 3),
                # Observation vs inference: the strongest kind of evidence behind the event, and the mix.
                "basis": min((o.basis for o in members), key=lambda b: BASIS_RANK[b]),
                "basis_counts": {b: n for b in BASIS_RANK if (n := sum(o.basis == b for o in members))},
            })
        drafts.sort(key=lambda d: (d["t_start_s"], CATEGORY_ORDER[d["category"]], d["label"]))
        return [
            TimelineEvent(id=f"evt_{i:02d}", timestamp=_ref() + timedelta(seconds=d["t_start_s"]), **d)
            for i, d in enumerate(drafts, start=1)
        ]

    def _extend_states(self, ranges):
        """Signal states last until the next state of the same approach; motion until the vehicle's next event."""
        signals: dict[str, list[_Cluster]] = {}
        for c in self.clusters:
            if c.family == "signal" and c.anchored:
                signals.setdefault(c.key[1], []).append(c)
        for seq in signals.values():
            seq.sort(key=lambda c: c.t)
            for a, b in zip(seq, seq[1:]):
                ranges[id(a)][1] = max(ranges[id(a)][1], b.t)
        self.signal_seq = signals

        for c in self.clusters:
            if c.family != "moving" or not c.anchored:
                continue
            later = [d.t for d in self.clusters
                     if d is not c and d.anchored and d.vehicles & c.vehicles and d.t > c.t
                     and d.family not in ("signal", "moving", "sensor_gap")]
            if later:
                ranges[id(c)][1] = max(ranges[id(c)][1], min(later))

    def _signal_at(self, approach: str | None, t: float) -> str | None:
        state = None
        for c in getattr(self, "signal_seq", {}).get(approach, []):
            if c.t < t:
                state = c.key[2]
        return state

    def _vehicle_descriptions(self) -> dict[str, str]:
        best: dict[str, tuple[bool, int, float, str]] = {}
        rank = {SourceType.sensor: 2, SourceType.video: 1, SourceType.witness: 0}
        for c in self.clusters:
            for o in c.members:
                if o.vehicle_id and o.vehicle_description:
                    desc = o.vehicle_description.split(",")[0].strip()
                    names_type = bool(VEHICLE_WORDS & set(desc.lower().split()))
                    score = (names_type, rank[o.source_type], o.confidence, desc)
                    if o.vehicle_id not in best or score[:3] > best[o.vehicle_id][:3]:
                        best[o.vehicle_id] = score
        return {v: s[3] for v, s in best.items()}

    def _category(self, c: _Cluster) -> str:
        if c.family == "signal":
            return "signal"
        if c.family == "collision":
            return "collision"
        if c.family == "post_collision":
            return "post_collision"
        if c.vehicles and c.family != "sensor_gap":
            return "vehicle_motion"
        return "other"

    def _label(self, c: _Cluster, descs: dict[str, str], t: float) -> str:
        vs = sorted(c.vehicles)
        v = vs[0] if len(vs) == 1 else None
        members = c.members
        attrs = [o.attributes for o in members]
        # Fixed cameras / video-derived series have no V1/V2 id: name what was seen and where.
        first = attrs[0] if attrs else {}
        view = first.get("view")
        where = "" if view is None else (f" (camera view {view})" if isinstance(view, int) else f" ({str(view).replace('_', ' ')})")
        who = " and ".join(vs) or (f"{first['label']}{where}" if first.get("label") else "Unidentified vehicle")
        if len(vs) == 1 and vs[0].startswith("U-") and first.get("label"):
            who = f"{first['label']}{where}"  # a provisional cross-source id reads worse than the description
        elif len(vs) > 1 and all(x.startswith("U-") for x in vs):
            # Several provisional vehicles (e.g. linked tracks on a fixed camera): name each by what was seen, not by id.
            names = [next((o.attributes["label"] for o in members if o.vehicle_id == x and o.attributes.get("label")), None) for x in vs]
            if all(names):
                who = (" and ".join(names) if len(set(names)) == len(names) else f"{len(names)} {names[0]}s") + where
        if not vs and members[0].action == "hazard":
            what = str(first.get("hazard", "hazard")).replace("_", " ")
            return f"Hazard: {what}{where}"
        if not vs and members[0].action == "motion_spike":
            return f"Motion spike{where}"

        if c.family == "signal":
            _, approach, state = c.key
            if not approach:
                return f"Signal {state or 'state unknown'} (approach not stated)"
            return f"{APPROACH_NAMES.get(approach, approach)} signal {state or 'unknown'}"
        if c.family == "collision":
            side = next((a["impact_side"] for a in attrs if a.get("impact_side")), None)
            # "between" needs two parties: an AI-reported collision may name only one vehicle
            return (f"Collision between {who}" if " and " in who or len(vs) > 1 else f"Collision involving {who}") + (f" ({side})" if side else "")
        if c.family == "post_collision":
            return f"{who} come to rest after the collision" if len(vs) > 1 else f"{who} comes to rest after the collision"

        name = f"{v} {descs[v]}" if v and v in descs else who
        approach = _approach_of(members)
        if c.family == "moving":
            ranked = sorted(members, key=lambda o: (o.source_type == SourceType.sensor, o.confidence), reverse=True)
            speed = next((o.attributes["speed_kmh"] for o in ranked if o.attributes.get("speed_kmh") is not None), None)
            heading = f" {APPROACH_NAMES[approach].lower()}" if approach else ""
            return f"{name}{heading}" + (f" at ~{round(float(speed))} km/h" if speed is not None else "")
        if c.family == "braking":
            return f"{who} begins braking"
        if c.family == "entering":
            label = f"{who} enters the junction"
            if approach:
                label += f" from the {APPROACH_FROM[approach]}"
                state = self._signal_at(approach, t)
                if state:
                    label += " against red" if state == "red" else f" on {state}"
            return label
        if c.family == "stopped":
            return f"{who} stopped"
        if c.family == "heading_change":
            deg = next((a["heading_change_deg"] for a in attrs if a.get("heading_change_deg") is not None), None)
            return f"{who} sudden heading change" + (f" ({deg:+g}°)" if deg is not None else "")
        if c.family == "sensor_gap":
            lo, hi = c.time_range()
            return f"{who} sensor data gap ({lo:g}–{hi:g} s)"
        verb = VERBS.get(members[0].action, members[0].action.replace("_", " "))
        only_witness = all(o.source_type == SourceType.witness for o in members)
        return f"{who} {verb}" + (" (witness claim)" if only_witness else "")


def build_timeline(observation_sets: list[ObservationSet]) -> Timeline:
    b = _Builder(observation_sets)
    b.cluster_anchored()
    b.attach_weak()
    b.place_untimed()
    return Timeline(
        incident_id=config.INCIDENT_ID,
        reference_time=_ref(),
        generated_at=datetime.now(timezone.utc),
        events=b.events(),
    )
