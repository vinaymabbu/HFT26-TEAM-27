import { useCallback, useEffect, useMemo, useState } from 'react'
import { API_BASE, api } from './api'
import ContradictionExplorer from './components/ContradictionExplorer'
import EventAnalysis from './components/EventAnalysis'
import EvidenceManager from './components/EvidenceManager'
import IncidentTimeline from './components/IncidentTimeline'
import Metrics from './components/Metrics'
import Overview from './components/Overview'
import ReconstructionViewer from './components/ReconstructionViewer'

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'evidence', label: 'Evidence' },
  { id: 'timeline', label: 'Timeline' },
  { id: 'contradictions', label: 'Contradictions' },
  { id: 'reconstruction', label: 'Reconstruction' },
  { id: 'metrics', label: 'Metrics' },
  { id: 'footage', label: 'Footage' },
]
const THEME_KEY = 'theme'
const initialTheme = () => {
  try {
    const saved = localStorage.getItem(THEME_KEY)
    if (saved === 'dark' || saved === 'light') return saved
  } catch { /* storage blocked: fall back to the system setting */ }
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}
const tabFromHash = () => {
  const id = window.location.hash.slice(1)
  return TABS.some((t) => t.id === id) ? id : 'overview'
}

export default function App() {
  const [results, setResults] = useState(null)
  const [run, setRun] = useState(null)
  const [error, setError] = useState(null)
  const [time, setTime] = useState(0)
  const [selectedObs, setSelectedObs] = useState([])
  const [tab, setTab] = useState(tabFromHash)
  const [analysing, setAnalysing] = useState(false)
  const [theme, setTheme] = useState(initialTheme)
  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    try { localStorage.setItem(THEME_KEY, theme) } catch { /* not persisted */ }
  }, [theme])

  const open = useCallback((id) => { window.location.hash = id }, [])
  // 1-7 jump between tabs (not while typing in a field).
  useEffect(() => {
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName) || e.target.isContentEditable) return
      const tab = TABS[Number(e.key) - 1]
      if (tab) open(tab.id)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])
  useEffect(() => {
    const onHash = () => setTab(tabFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const loadResults = useCallback(async () => {
    try {
      const [observations, timeline, conflicts, reconstruction, eventAnalysis, latest] = await Promise.all([
        api.observations(), api.timeline(), api.conflicts(), api.reconstruction(), api.eventAnalysis(), api.latestRun(),
      ])
      setResults({ observations, timeline, conflicts, reconstruction, eventAnalysis })
      // One clock for every panel: clamp to whatever the views cover.
      const spans = [reconstruction, eventAnalysis].filter((r) => r && r.t_max_s > r.t_min_s)
      if (spans.length) {
        const lo = Math.min(...spans.map((r) => r.t_min_s))
        const hi = Math.max(...spans.map((r) => r.t_max_s))
        setTime((t) => Math.min(Math.max(t, lo), hi))
      }
      setRun(latest)
      setError(null)
    } catch (e) {
      setError(`Could not reach the API: ${e.message}. Is the backend running on :8000?`)
    }
  }, [])

  useEffect(() => { loadResults() }, [loadResults])

  // Results on screen can go stale: a newer run finished (another tab, the CLI) or evidence it used was deleted.
  // Check on window focus and every 15 s; the user reloads explicitly so panels never change under them.
  const [newerRun, setNewerRun] = useState(null)
  const [missingEvidence, setMissingEvidence] = useState([])
  useEffect(() => {
    if (!run) return
    let cancelled = false
    const check = async () => {
      try {
        const [latest, evidence] = await Promise.all([api.latestRun(), api.listEvidence()])
        if (cancelled) return
        setNewerRun(latest && latest.run_id !== run.run_id ? latest : null)
        const present = new Set(evidence.map((e) => e.id))
        setMissingEvidence((run.evidence ?? []).filter((e) => !present.has(e.id)))
      } catch { /* API unreachable: loadResults reports that */ }
    }
    check()
    const onFocus = () => check()
    const onVisible = () => { if (document.visibilityState === 'visible') check() }
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisible)
    const timer = setInterval(check, 15000)
    return () => {
      cancelled = true
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisible)
      clearInterval(timer)
    }
  }, [run])

  const observationsById = useMemo(() => {
    const map = {}
    for (const set of results?.observations ?? []) for (const o of set.observations) map[o.id] = o
    return map
  }, [results])

  const focus = (t, obsIds = []) => {
    if (t != null) setTime(t)
    setSelectedObs(obsIds)
  }

  const counts = {
    evidence: results?.observations.length,
    timeline: results?.timeline.events.length,
    contradictions: results?.conflicts.conflicts.length,
    reconstruction: results?.reconstruction?.vehicles?.length,
    footage: results?.eventAnalysis ? '●' : null,
  }
  const high = results?.conflicts.conflicts.filter((c) => c.severity === 'high').length ?? 0
  const unresolved = results?.conflicts.conflicts.length ?? 0 // nothing resolves a conflict automatically
  const confirmed = results?.timeline && run
  const schematic = results?.reconstruction?.scene?.units === 'schematic'

  // Every tab stays mounted (hidden when inactive) so a running analysis and replay state survive tab changes.
  const pane = (id, node) => <div key={id} hidden={tab !== id}>{node}</div>

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 bg-[#0f172a] text-[#fff]">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-2 px-4 py-3">
          <div className="min-w-0">
            <h1 className="text-lg font-semibold leading-tight">Incident Reconstruction</h1>
            <p className="truncate text-xs text-[#94a3b8]">
              {results?.timeline.incident_id ?? '—'} · reference time {results?.timeline.reference_time ?? '—'}
            </p>
          </div>
          <div className="flex items-center gap-3 text-xs text-[#cbd5e1]">
            {analysing && <span className="flex items-center gap-1.5 rounded-full bg-[#0ea5e9]/20 px-2.5 py-1 text-[#bae6fd]">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#7dd3fc]" />Analysing…</span>}
            {run && <span>last run {run.run_id}</span>}
            <a href={`${API_BASE}/api/report?format=markdown&narrative=false`} target="_blank" rel="noreferrer" title="Open the investigator report (Markdown, with citations)"
              className="rounded-full border border-[#334155] px-2.5 py-1 hover:bg-[#1e293b]">Report</a>
            <button onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')} role="switch" aria-checked={theme === 'dark'}
              aria-label="Dark mode" title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
              className="flex items-center gap-1.5 rounded-full border border-[#334155] px-2.5 py-1 hover:bg-[#1e293b]">
              <span aria-hidden>{theme === 'dark' ? '☾' : '☀'}</span>
              <span className="relative h-4 w-7 rounded-full bg-[#334155]">
                <span className={`absolute top-0.5 h-3 w-3 rounded-full bg-[#fff] transition-all ${theme === 'dark' ? 'left-3.5' : 'left-0.5'}`} />
              </span>
            </button>
          </div>
        </div>
        <nav className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-3" role="tablist" aria-label="Dashboard sections">
          {TABS.map((t) => {
            const active = tab === t.id
            const n = counts[t.id]
            return (
              <button key={t.id} role="tab" aria-selected={active} onClick={() => open(t.id)} title={`${t.label} (press ${TABS.indexOf(t) + 1})`}
                className={`flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2 text-sm transition-colors ${active ? 'border-sky-400 text-[#fff]' : 'border-transparent text-[#94a3b8] hover:text-[#e2e8f0]'}`}>
                {t.label}
                {n != null && n !== 0 && (
                  <span className={`rounded-full px-1.5 text-[0.7rem] font-medium leading-4 ${t.id === 'contradictions' && high ? 'bg-[#ef4444] text-[#fff]' : 'bg-[#334155] text-[#e2e8f0]'}`}>{n}</span>
                )}
              </button>
            )
          })}
        </nav>
      </header>

      <div className="border-b border-amber-200 bg-amber-50 px-4 py-1.5 text-center text-xs text-amber-900">
        <b>Evidence-supported reconstruction, not a legal finding.</b> Confidence shows how well the evidence supports an item,
        not the probability that it happened.
        {unresolved > 0 && <button onClick={() => open('contradictions')} className="ml-2 font-semibold underline">
          {unresolved} unresolved contradiction{unresolved === 1 ? '' : 's'} / gap{unresolved === 1 ? '' : 's'}</button>}
      </div>

      <main className="mx-auto max-w-7xl space-y-4 px-4 py-4 *:min-w-0">
        {error && <div className="rounded-md bg-red-50 p-3 text-sm text-red-700">{error}</div>}
        {!analysing && (newerRun || missingEvidence.length > 0) && (
          <div role="alert" className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900">
            <span className="min-w-0 flex-1">
              <b>{newerRun ? 'Newer results available.' : 'These results are stale.'}</b>{' '}
              {newerRun && <>You are viewing run <span className="font-mono">{run.run_id}</span>; run{' '}
                <span className="font-mono">{newerRun.run_id}</span> has finished since. </>}
              {missingEvidence.length > 0 && <>Run <span className="font-mono">{run.run_id}</span> used evidence that has
                since been deleted: {missingEvidence.map((e) => `${e.filename} (${e.id})`).join(', ')}.
                {!newerRun && ' Re-run the analysis.'}</>}
            </span>
            {newerRun
              ? <button onClick={loadResults} className="rounded bg-amber-600 px-3 py-1 text-xs font-medium text-white hover:bg-amber-700">Reload</button>
              : <button onClick={() => open('evidence')} className="rounded bg-amber-600 px-3 py-1 text-xs font-medium text-white hover:bg-amber-700">Go to Evidence</button>}
          </div>
        )}
        {pane('overview', <Overview results={results} run={run} onOpen={open} />)}
        {pane('evidence', <div className="mx-auto max-w-2xl"><EvidenceManager run={run} observations={results?.observations}
          onAnalysisDone={loadResults} onRunningChange={setAnalysing} /></div>)}
        {pane('timeline', <IncidentTimeline timeline={results?.timeline} time={time} selectedObs={selectedObs} onFocus={focus}
          observationsById={observationsById} eventAnalysis={results?.eventAnalysis} conflicts={results?.conflicts} />)}
        {pane('contradictions', <ContradictionExplorer report={results?.conflicts} observationsById={observationsById} onFocus={focus} />)}
        {pane('reconstruction', <ReconstructionViewer data={results?.reconstruction} time={time} onTimeChange={setTime}
          timeline={results?.timeline} conflicts={results?.conflicts} observationsById={observationsById} onFocus={focus} />)}
        {pane('metrics', <Metrics results={results} run={run} onOpen={open} />)}
        {pane('footage', <EventAnalysis data={results?.eventAnalysis} time={time} onTimeChange={setTime} />)}
      </main>
      <footer className="mx-auto max-w-7xl px-4 pb-6 text-[0.75rem] text-slate-400">
        {confirmed && schematic && 'Reconstruction is a schematic estimate. '}Observations are not legal conclusions.
      </footer>
    </div>
  )
}
