import ObservationList from './ObservationList'

const SOURCE_LABELS = {
  measured: 'measured at a sensor sample',
  tracked: 'tracked in video',
  interpolated: 'interpolated between samples',
  extrapolated: 'extrapolated beyond samples',
  estimated: 'estimated (position assumed, not measured)',
}
// Observed: read from a sensor sample or tracked in video. Everything else is an inference.
export const isObservedSource = (src) => src === 'measured' || src === 'tracked'
const pct = (c) => (c == null ? '—' : `${Math.round(c * 100)}%`)

// Mean confidence and point-source mix of one or more tracks.
export function trackStats(vehicles) {
  const counts = {}
  let sum = 0
  let n = 0
  for (const v of vehicles) {
    for (const p of v.track) {
      counts[p.source ?? 'estimated'] = (counts[p.source ?? 'estimated'] ?? 0) + 1
      if (p.confidence != null) { sum += p.confidence; n += 1 }
    }
  }
  const total = Object.values(counts).reduce((a, b) => a + b, 0)
  const observed = (counts.measured ?? 0) + (counts.tracked ?? 0)
  return { counts, total, mean: n ? sum / n : null, observed: total ? observed / total : 0 }
}

function ConfidenceBar({ value }) {
  const c = value ?? 0
  const color = c >= 0.7 ? 'bg-emerald-500' : c >= 0.4 ? 'bg-amber-500' : 'bg-rose-400'
  return (
    <div className="h-1.5 w-full rounded bg-slate-100" title={`confidence ${pct(value)}`}>
      <div className={`h-1.5 rounded ${color}`} style={{ width: `${c * 100}%` }} />
    </div>
  )
}

// Share of observed vs inferred points: the split the problem statement asks to keep visible.
export function ObservedInferred({ stats }) {
  const o = Math.round(stats.observed * 100)
  return (
    <div>
      <div className="flex h-2 overflow-hidden rounded bg-slate-100" title={`${o}% observed, ${100 - o}% inferred`}>
        <div className="bg-emerald-500" style={{ width: `${o}%` }} />
        <div className="bg-violet-400 opacity-70" style={{ width: `${100 - o}%`, backgroundImage: 'repeating-linear-gradient(45deg,transparent 0 3px,rgba(255,255,255,.55) 3px 5px)' }} />
      </div>
      <div className="mt-1 flex justify-between text-[0.75rem]">
        <span className="text-emerald-700">{o}% observed</span>
        <span className="text-violet-700">{100 - o}% inferred</span>
      </div>
    </div>
  )
}

// When, along the replay window, one vehicle's position is observed vs inferred.
function EvidenceStrip({ vehicle, data, time }) {
  const span = data.t_max_s - data.t_min_s || 1
  const pos = (t) => ((t - data.t_min_s) / span) * 100
  const runs = []
  const tr = vehicle.track
  for (let i = 0; i < tr.length; i++) {
    const obs = isObservedSource(tr[i].source ?? 'estimated')
    const end = i + 1 < tr.length ? tr[i + 1].t_s : tr[i].t_s
    const last = runs.at(-1)
    if (last && last.obs === obs) last.end = end
    else runs.push({ obs, start: tr[i].t_s, end })
  }
  return (
    <div>
      <div className="relative h-3 overflow-hidden rounded bg-slate-100">
        {runs.map((r, i) => (
          <div key={i} className="absolute top-0 h-3" title={`${r.obs ? 'observed' : 'inferred'} t=${r.start.toFixed(1)}–${r.end.toFixed(1)}s`}
            style={{ left: `${pos(r.start)}%`, width: `max(2px, ${pos(r.end) - pos(r.start)}%)`, background: r.obs ? 'var(--color-emerald-500)' : 'var(--color-violet-500)',
              opacity: r.obs ? 1 : 0.55, backgroundImage: r.obs ? undefined : 'repeating-linear-gradient(45deg,transparent 0 3px,rgba(255,255,255,.55) 3px 5px)' }} />
        ))}
        <div className="absolute inset-y-0 w-px bg-red-500" style={{ left: `${Math.min(100, Math.max(0, pos(time)))}%` }} />
      </div>
      <div className="mt-0.5 flex justify-between text-[0.7rem] text-slate-400"><span>{data.t_min_s.toFixed(0)}s</span><span>{data.t_max_s.toFixed(0)}s</span></div>
    </div>
  )
}

function EvidenceGroups({ ids, observationsById }) {
  const known = ids.slice(0, 8)
  const observed = known.filter((id) => ['measured', 'derived'].includes(observationsById[id]?.basis))
  const rest = known.filter((id) => !observed.includes(id))
  return (
    <div className="space-y-2">
      {observed.length > 0 && (
        <div>
          <h5 className="mb-1 font-medium text-emerald-700">Observed <span className="font-normal text-slate-400">· measured or computed by fixed rules</span></h5>
          <ObservationList ids={observed} observationsById={observationsById} />
        </div>
      )}
      {rest.length > 0 && (
        <div>
          <h5 className="mb-1 font-medium text-violet-700">Inferred or reported <span className="font-normal text-slate-400">· an AI reading or a statement</span></h5>
          <ObservationList ids={rest} observationsById={observationsById} />
        </div>
      )}
      {ids.length > 8 && <p className="text-slate-400">+{ids.length - 8} more in the positions export</p>}
    </div>
  )
}

function SourceMix({ stats }) {
  return (
    <ul className="space-y-0.5 text-[0.75rem] text-slate-600">
      {Object.entries(stats.counts).map(([src, n]) => (
        <li key={src} className="flex justify-between gap-2">
          <span>{SOURCE_LABELS[src] ?? src}</span>
          <span className="font-mono text-slate-500">{Math.round((n / stats.total) * 100)}%</span>
        </li>
      ))}
    </ul>
  )
}

// What the map is built from, how far to trust it, and the evidence behind whatever is selected.
export default function ReconstructionTrace({ data, time, selected, onSelect, position, moments, observationsById, onSeek, schematic, onFocus }) {
  const all = trackStats(data.vehicles)
  const vehicle = data.vehicles.find((v) => v.vehicle_id === selected)
  const one = vehicle ? trackStats([vehicle]) : null
  const active = moments.filter((m) => m.t <= time + 1e-6).at(-1)

  return (
    <aside className="flex min-w-0 flex-col gap-3 text-xs">
      <section className="rounded border border-slate-200 p-2.5">
        <h3 className="mb-1 font-semibold text-slate-700">How far to trust this map</h3>
        <div className="mb-1 flex items-baseline justify-between">
          <span className="text-slate-500">Mean point confidence</span>
          <span className="font-mono font-semibold">{pct(all.mean)}</span>
        </div>
        <ConfidenceBar value={all.mean} />
        <h4 className="mb-1 mt-3 font-medium text-slate-600">Observed vs inferred</h4>
        <ObservedInferred stats={all} />
        <div className="mt-2"><SourceMix stats={all} /></div>
        <p className="mt-2 text-[0.75rem] leading-snug text-slate-500">
          {schematic
            ? 'Only who moved where relative to whom, and in what order, is supported. Distances, speeds and any impact point are not.'
            : 'Solid line = actual path from real samples; dotted = inferred, filled in between. A map is a reconstruction of what may have happened, not a legal finding.'}
        </p>
      </section>

      {data.collision && (
        <section className="rounded border border-red-300 p-2.5">
          <h3 className="mb-1 font-semibold text-red-700">Inferred collision <span className="font-normal text-slate-400">· t={data.collision.t_s}s · conf {pct(data.collision.confidence)}</span></h3>
          <p className="mb-1.5 text-[0.75rem] text-slate-500">Not observed directly. Placed where the tracks come closest, supported by:</p>
          <ObservationList ids={data.collision.supporting_observation_ids.slice(0, 4)} observationsById={observationsById} />
        </section>
      )}

      <section className="rounded border border-slate-200 p-2.5">
        <h3 className="mb-1 font-semibold text-slate-700">Trace a vehicle</h3>
        {!vehicle && <p className="text-[0.75rem] text-slate-500">Click a vehicle on the map or in the legend to see the evidence it is built from.</p>}
        {vehicle && (
          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-1.5 font-medium text-slate-800">
                <span className="inline-block h-2.5 w-3.5 rounded-sm" style={{ background: vehicle.color }} />{vehicle.label}
              </span>
              <button onClick={() => onSelect(null)} className="text-[0.75rem] text-slate-400 hover:text-slate-700">clear</button>
            </div>
            {vehicle.description && <p className="text-slate-500">{vehicle.description}</p>}
            <dl className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-[0.75rem]">
              <dt className="text-slate-500">Seen</dt>
              <dd className="font-mono">t={vehicle.track[0].t_s.toFixed(1)}–{vehicle.track.at(-1).t_s.toFixed(1)}s</dd>
              <dt className="text-slate-500">Now ({time.toFixed(1)}s)</dt>
              <dd className="font-mono">{position ? `${position.source ?? 'estimated'} · ${pct(position.confidence)}` : 'not in view'}</dd>
              <dt className="text-slate-500">Speed now</dt>
              <dd className="font-mono">{position?.speed_kmh != null ? `${position.speed_kmh.toFixed(0)} km/h` : 'not measurable'}</dd>
              <dt className="text-slate-500">Mean confidence</dt>
              <dd className="font-mono">{pct(one.mean)}</dd>
            </dl>
            <ConfidenceBar value={one.mean} />
            <h4 className="font-medium text-slate-600">Observed vs inferred</h4>
            <ObservedInferred stats={one} />
            <EvidenceStrip vehicle={vehicle} data={data} time={time} />
            <SourceMix stats={one} />
            <div>
              <h4 className="mb-1 font-medium text-slate-600">Supporting evidence</h4>
              {vehicle.source_observation_ids.length
                ? <EvidenceGroups ids={vehicle.source_observation_ids} observationsById={observationsById} />
                : <p className="text-slate-400">No observation ids recorded for this track.</p>}
            </div>
            <div>
              <h4 className="mb-1 font-medium text-slate-600">What was added to get these positions</h4>
              {vehicle.assumptions?.length
                ? <ul className="list-disc space-y-0.5 pl-4 text-[0.75rem] text-violet-800">{vehicle.assumptions.map((a) => <li key={a}>{a}</li>)}</ul>
                : <p className="text-[0.75rem] text-emerald-700">Nothing: these positions are observed directly in the evidence.</p>}
            </div>
          </div>
        )}
      </section>

      <section className="rounded border border-slate-200 p-2.5">
        <h3 className="mb-1 font-semibold text-slate-700">Key moments <span className="font-normal text-slate-400">({moments.length})</span></h3>
        {moments.length === 0 && <p className="text-[0.75rem] text-slate-500">No timeline events or contradictions to jump to.</p>}
        <ul className="max-h-56 space-y-0.5 overflow-y-auto">
          {moments.map((m) => (
            <li key={m.id}>
              <button onClick={() => { onSeek(m.t); onFocus?.(m.t, m.observationIds) }}
                className={`flex w-full items-start gap-1.5 rounded px-1 py-0.5 text-left hover:bg-slate-50 ${active?.id === m.id ? 'bg-yellow-50 ring-1 ring-yellow-300' : ''}`}>
                <span className={`mt-1 inline-block h-2 w-2 shrink-0 rounded-full ${m.kind === 'conflict' ? 'bg-amber-500' : m.kind === 'collision' ? 'bg-red-600' : 'bg-slate-400'}`} />
                <span className="min-w-0">
                  <span className="font-mono text-slate-400">t={m.t.toFixed(1)}s </span>
                  <span className="text-slate-700">{m.label}</span>
                  <span className="ml-1 text-[0.7rem] uppercase text-slate-400">{m.kind === 'conflict' ? 'contradiction' : m.basis ?? ''}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    </aside>
  )
}
