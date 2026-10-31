// Pure helpers behind the Metrics tab and the observed-vs-inferred chart. They only read what the API already
// returned; nothing here measures or decides anything new.

export const mean = (xs) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null)
export const pct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`)

// Same cut-offs everywhere a confidence is coloured.
export const band = (c) => (c >= 0.7 ? 'high' : c >= 0.4 ? 'medium' : 'low')
export const BAND_TEXT = { high: 'text-emerald-700', medium: 'text-amber-700', low: 'text-rose-600' }
export const BAND_BAR = { high: 'bg-emerald-500', medium: 'bg-amber-500', low: 'bg-rose-400' }

// Counts per fifth of the 0-1 range: [0-20%), [20-40%), ... [80-100%].
export function histogram(values, bins = 5) {
  const out = new Array(bins).fill(0)
  for (const v of values) out[Math.min(bins - 1, Math.max(0, Math.floor(v * bins)))] += 1
  return out
}

// Observed = read from a sensor sample or tracked in video; everything else is an inference.
export const OBSERVED_SOURCES = ['measured', 'tracked']
export const isObservedSource = (src) => OBSERVED_SOURCES.includes(src)
export const SOURCE_LABELS = {
  measured: 'measured at a sensor sample',
  tracked: 'tracked in video',
  interpolated: 'interpolated between samples',
  extrapolated: 'extrapolated beyond samples',
  estimated: 'estimated (position assumed, not measured)',
}

// Consecutive track points with the same observed/inferred status, as time runs.
export function evidenceRuns(track) {
  const runs = []
  for (let i = 0; i < track.length; i++) {
    const observed = isObservedSource(track[i].source ?? 'estimated')
    const end = i + 1 < track.length ? track[i + 1].t_s : track[i].t_s
    const last = runs.at(-1)
    if (last && last.observed === observed) last.end = end
    else runs.push({ observed, start: track[i].t_s, end })
  }
  return runs
}

// Mean confidence, observed share and source mix of a set of tracks.
export function trackSummary(vehicles) {
  const counts = {}
  let sum = 0
  let n = 0
  for (const v of vehicles) {
    for (const p of v.track) {
      const src = p.source ?? 'estimated'
      counts[src] = (counts[src] ?? 0) + 1
      if (p.confidence != null) { sum += p.confidence; n += 1 }
    }
  }
  const total = Object.values(counts).reduce((a, b) => a + b, 0)
  const observed = OBSERVED_SOURCES.reduce((a, k) => a + (counts[k] ?? 0), 0)
  return { counts, total, mean: n ? sum / n : null, observed: total ? observed / total : null }
}

export const flatObservations = (sets) => (sets ?? []).flatMap((s) => s.observations)
