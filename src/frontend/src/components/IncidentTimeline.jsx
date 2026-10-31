import { useState } from 'react'
import EventFrame from './EventFrame'
import ObservationList from './ObservationList'
import Panel, { BasisBadge, SourceBadge } from './Panel'

const CATEGORY_COLORS = {
  vehicle_motion: 'bg-blue-500',
  signal: 'bg-amber-500',
  collision: 'bg-red-600',
  post_collision: 'bg-violet-500',
  other: 'bg-slate-400',
}

// Open contradictions or gaps that touch an event: they cite one of its observations or fall inside its time window.
function flagsFor(event, conflicts) {
  return (conflicts?.conflicts ?? []).filter((c) => c.observation_ids.some((id) => event.observation_ids.includes(id))
    // a long-running event (a signal held red for 20 s) overlaps everything, so time alone only counts for short ones
    || (event.t_end_s - event.t_start_s <= 5 && c.t_range_s && c.t_range_s[0] <= event.t_end_s + 1e-6 && c.t_range_s[1] >= event.t_start_s - 1e-6))
}

export default function IncidentTimeline({ timeline, time, selectedObs, onFocus, observationsById, eventAnalysis, conflicts }) {
  const [selected, setSelected] = useState(null)
  const [sources, setSources] = useState([]) // empty = every source
  const [flaggedOnly, setFlaggedOnly] = useState(false)
  const [query, setQuery] = useState('')
  if (!timeline) return <Panel title="Incident Timeline">Loading…</Panel>

  const events = [...timeline.events].sort((a, b) => a.t_start_s - b.t_start_s)
  if (events.length === 0) {
    return <Panel title="Incident Timeline"><p className="text-xs text-slate-400">No events: nothing in the evidence could be placed in time.</p></Panel>
  }
  // Axis covers the incident window; long-running context (e.g. a signal red since t=-20) is clamped.
  const tMin = Math.min(0, ...events.map((e) => e.t_start_s).filter((t) => t > -5))
  const tMax = Math.max(...events.map((e) => e.t_end_s), 1)
  const frac = (t) => (Math.min(Math.max(t, tMin), tMax) - tMin) / (tMax - tMin)
  const pct = (t) => `${frac(t) * 100}%`
  const current = events.find((e) => e.id === selected)
  const q = query.trim().toLowerCase()
  const shown = events.filter((e) => (!sources.length || e.source_types.some((s) => sources.includes(s)))
    && (!flaggedOnly || flagsFor(e, conflicts).length > 0) && (!q || e.label.toLowerCase().includes(q)))
  const toggleSource = (s) => setSources(sources.includes(s) ? sources.filter((x) => x !== s) : [...sources, s])

  const pick = (e) => {
    const closing = selected === e.id
    setSelected(closing ? null : e.id)
    onFocus(e.t_start_s, closing ? [] : e.observation_ids)
  }

  return (
    <Panel title={`Incident Timeline (${events.length})`}>
      {(conflicts?.conflicts.length ?? 0) > 0 && (
        <div className="mb-3 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <b>{conflicts.conflicts.length} unresolved</b> contradiction{conflicts.conflicts.length === 1 ? '' : 's'} or gap{conflicts.conflicts.length === 1 ? '' : 's'} in the evidence.
          Events they touch are flagged <span className="font-semibold">⚠</span> below.
          <a href="#contradictions" className="ml-1 font-semibold underline">Open the Contradiction Explorer</a>
        </div>
      )}
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
        <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search events…" aria-label="Search events"
          className="w-44 rounded border border-slate-200 bg-white px-2 py-1" />
        {['sensor', 'video', 'witness'].map((s) => (
          <button key={s} onClick={() => toggleSource(s)} aria-pressed={sources.includes(s)}
            className={`rounded-full border px-2.5 py-0.5 capitalize ${sources.includes(s) ? 'border-slate-900 bg-slate-900 text-white' : 'border-slate-200 text-slate-600 hover:bg-slate-50'}`}>{s}</button>
        ))}
        {(conflicts?.conflicts.length ?? 0) > 0 && (
          <label className="flex items-center gap-1 text-slate-600"><input type="checkbox" checked={flaggedOnly} onChange={(e) => setFlaggedOnly(e.target.checked)} />Only events with open issues</label>
        )}
        <span className="ml-auto text-slate-400">{shown.length === events.length ? `${events.length} events` : `${shown.length} of ${events.length} shown`}</span>
        {(sources.length > 0 || flaggedOnly || q) && <button onClick={() => { setSources([]); setFlaggedOnly(false); setQuery('') }} className="text-sky-700 hover:underline">Clear</button>}
      </div>
      {shown.length === 0 && <p className="rounded border border-dashed border-slate-300 p-3 text-xs text-slate-500">No events match these filters.</p>}
      <ul className="space-y-1">
        {shown.map((e) => {
          const highlighted = e.id === selected || e.observation_ids.some((id) => selectedObs.includes(id))
          const flags = flagsFor(e, conflicts)
          return (
            <li key={e.id} onClick={() => pick(e)}
              className={`flex cursor-pointer flex-col gap-1 rounded px-1 py-1 hover:bg-slate-50 sm:flex-row sm:items-center ${highlighted ? 'bg-yellow-50 ring-1 ring-yellow-300' : ''}`}>
              <div className="min-w-0 sm:w-64 sm:shrink-0 sm:pr-3">
                <div className="text-xs font-medium">{e.label}</div>
                <div className="mt-0.5 flex flex-wrap items-center gap-1 text-[0.7rem] text-slate-500">
                  t={e.t_start_s}s{e.t_end_s !== e.t_start_s && `–${e.t_end_s}s`}
                  {e.uncertainty_s > 0 && ` ±${e.uncertainty_s}s`}
                  {e.source_types.map((s) => <SourceBadge key={s} type={s} />)}
                  <BasisBadge basis={e.basis} />
                  <span className={`font-medium ${e.confidence >= 0.7 ? 'text-emerald-700' : e.confidence >= 0.4 ? 'text-amber-700' : 'text-rose-600'}`}
                    title="How well the evidence supports this event">conf {Math.round(e.confidence * 100)}%</span>
                  <span className="text-slate-500"
                    title={`On observations alone (measured, derived or witness-reported) the event stands at ${Math.round((e.confidence_observed ?? 0) * 100)}%. The rest of its confidence rests on inference, the AI model's readings.`}>
                    · observed {Math.round((e.confidence_observed ?? 0) * 100)}%</span>
                  {flags.length > 0 && (
                    <a href="#contradictions" onClick={(ev) => ev.stopPropagation()} title={flags.map((f) => f.title).join('\n')}
                      className="rounded bg-amber-100 px-1.5 py-0.5 font-medium text-amber-800">⚠ {flags.length} open</a>
                  )}
                </div>
              </div>
              <div className="relative h-3 w-full flex-1 rounded bg-slate-100" title={`confidence ${e.confidence}`}>
                <div className={`absolute top-0 h-3 rounded ${CATEGORY_COLORS[e.category]}`}
                  style={{ left: pct(e.t_start_s), width: `max(4px, calc(${pct(e.t_end_s)} - ${pct(e.t_start_s)}))`, opacity: 0.4 + 0.6 * e.confidence }} />
                <div className="pointer-events-none absolute -top-1 -bottom-1 w-px bg-red-500" style={{ left: pct(time) }} />
              </div>
            </li>
          )
        })}
      </ul>
      <div className="mt-1 flex justify-between text-[0.7rem] text-slate-400 sm:pl-64">
        <span>{tMin}s</span><span>{tMax}s</span>
      </div>

      {current && (
        <div className="mt-3 rounded border border-slate-200 p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <h3 className="text-xs font-semibold text-slate-700">Evidence for “{current.label}”</h3>
            <span className="shrink-0 text-[0.7rem] text-slate-400"
              title="Confidence on observations alone leaves out the AI model's readings; the gap is what rests on inference.">
              confidence {Math.round(current.confidence * 100)}% · on observation alone {Math.round((current.confidence_observed ?? 0) * 100)}%
              {current.confidence_observed < current.confidence - 0.005 && ` · ${Math.round((current.confidence - current.confidence_observed) * 100)} pts rest on inference`}
            </span>
          </div>
          <EventFrame event={current} observationsById={observationsById} eventAnalysis={eventAnalysis} />
          <ObservationList ids={current.observation_ids} observationsById={observationsById} />
        </div>
      )}
    </Panel>
  )
}
