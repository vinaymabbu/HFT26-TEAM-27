import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'
import Panel, { SourceBadge } from './Panel'

const STATUS_STYLES = {
  pending: 'bg-slate-50 text-slate-400',
  running: 'bg-sky-100 text-sky-800',
  real: 'bg-emerald-100 text-emerald-800',
  fixture: 'bg-slate-100 text-slate-600',
  skipped: 'bg-amber-100 text-amber-800',
  error: 'bg-red-100 text-red-700',
}
const FINISHED = new Set(['real', 'fixture', 'skipped', 'error'])

// One line about what the upload check learned (layout and rows, words, duration).
function evidenceNote(e) {
  const d = e.details ?? {}
  if (e.kind === 'sensor' && d.layout_name) return `${d.layout_name} · ${d.rows} rows${d.columns_found?.speed ? ` · speed: ${d.columns_found.speed}` : ''}`
  if (e.kind === 'witness' && d.words != null) return `${d.words} words · ${d.lines} lines`
  if (e.kind === 'video' && d.duration_s != null) return `${d.duration_s}s · ${d.width}×${d.height}`
  return null
}

function elapsed(from, to) {
  const s = Math.max(0, Math.round(((to ? new Date(to) : new Date()) - new Date(from)) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}

export default function EvidenceManager({ run, observations, onAnalysisDone, onRunningChange }) {
  const [evidence, setEvidence] = useState([])
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [job, setJob] = useState(null)
  const [message, setMessage] = useState(null)
  const [, tick] = useState(0)
  const inputRef = useRef(null)
  const running = job?.state === 'running'
  useEffect(() => { onRunningChange?.(running) }, [running, onRunningChange])

  const refresh = () => api.listEvidence().then(setEvidence).catch(() => {})
  useEffect(() => { refresh() }, [])

  // Poll the background job while it runs; pick up a run already in progress on page load.
  const poll = useCallback(async () => {
    try {
      const j = await api.currentJob()
      setJob(j)
      if (j?.state === 'running') return
      if (j?.state === 'failed') setMessage(j.error)
      if (j) await onAnalysisDone()
    } catch (e) {
      setMessage(e.message)
    }
  }, [onAnalysisDone])

  useEffect(() => {
    api.currentJob().then((j) => { if (j?.state === 'running') setJob(j) }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!running) return
    const id = setInterval(() => { poll(); tick((n) => n + 1) }, 1000)
    return () => clearInterval(id)
  }, [running, poll])

  const upload = async (files) => {
    setUploading(true)
    setMessage(null)
    try {
      for (const f of files) await api.uploadEvidence(f)
    } catch (e) {
      setMessage(e.message)
    }
    if (inputRef.current) inputRef.current.value = ''
    await refresh()
    setUploading(false)
  }

  const remove = async (id) => {
    const e = evidence.find((x) => x.id === id)
    if (!window.confirm(`Remove ${e?.ref ?? ''} ${e?.filename ?? 'this file'}? Its stored copy is deleted; the removal is logged.`)) return
    await api.deleteEvidence(id)
    refresh()
  }

  const analyze = async () => {
    setMessage(null)
    // Hosted serverless, the run happens inside this request and the answer is the finished job: show progress meanwhile.
    setJob({ state: 'running', started_at: new Date().toISOString(), stages: [] })
    try {
      const started = await api.startJob()
      setJob(started)
      if (started.state === 'failed') setMessage(started.error)
      if (started.state !== 'running') await onAnalysisDone() // finished inside the request: the response is the result
    } catch (e) {
      setJob(null)
      setMessage(e.message)
    }
  }

  const stages = running || job?.state ? job.stages : run?.stages.map((s) => ({ ...s, note: s.message })) ?? []
  const finished = stages.filter((s) => FINISHED.has(s.status)).length
  const active = stages.find((s) => s.status === 'running' && s.total > 0)
  // Overall progress: finished stages, plus the running stage's own fraction.
  const overall = stages.length ? (finished + (active ? active.done / active.total : 0)) / stages.length : 0
  const notes = (observations ?? []).flatMap((set) => set.warnings.map((w) => ({ w, type: set.source_type })))

  return (
    <Panel title="Evidence Manager">
      <label onDragOver={(e) => { e.preventDefault(); if (!running && !uploading) setDragging(true) }} onDragLeave={() => setDragging(false)}
        onDrop={(e) => { e.preventDefault(); setDragging(false); if (!running && !uploading) upload([...e.dataTransfer.files]) }}
        className={`block rounded-md border-2 border-dashed p-4 text-center text-sm text-slate-500 ${dragging ? 'border-sky-500 bg-sky-50' : 'border-slate-300'} ${running || uploading ? 'opacity-50' : 'cursor-pointer hover:border-slate-400'}`}>
        <input ref={inputRef} type="file" multiple accept=".mp4,.mov,.avi,.mkv,.txt,.csv" className="hidden"
          disabled={running || uploading} onChange={(e) => upload([...e.target.files])} />
        {uploading ? 'Uploading…' : 'Drop or choose files — video, witness .txt, sensor .csv'}
      </label>

      <ul className="mt-3 space-y-1.5">
        {evidence.length === 0
          ? <li className="text-xs text-slate-400">No evidence uploaded — analysis runs on demo fixtures.</li>
          : <li className="text-xs text-slate-400">Results use only your evidence; stages that can't run are skipped.</li>}
        {evidence.map((e) => (
          <li key={e.id} className="min-w-0 text-sm">
            <div className="flex min-w-0 items-center gap-2">
            {e.ref && <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[0.7rem] text-slate-600" title="evidence id">{e.ref}</span>}
            <SourceBadge type={e.kind} />
            <span className="min-w-0 flex-1 truncate" title={`${e.filename} · sha256 ${e.sha256 ?? 'n/a'}`}>{e.filename}</span>
            <span className="shrink-0 text-xs text-slate-400">{(e.size_bytes / 1024 / 1024).toFixed(1)} MB</span>
            <button onClick={() => remove(e.id)} disabled={running}
              className="shrink-0 text-xs text-slate-400 hover:text-red-600 disabled:opacity-40">remove</button>
            </div>
            {evidenceNote(e) && <p className="mt-0.5 pl-1 text-[0.75rem] text-slate-400">{evidenceNote(e)}</p>}
            {(e.details?.upload_warnings ?? []).map((w) => <p key={w} className="mt-0.5 pl-1 text-[0.75rem] text-amber-700">⚠ {w}</p>)}
          </li>
        ))}
      </ul>

      <button onClick={analyze} disabled={running || uploading}
        className="mt-4 w-full rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50">
        {running ? `Analysing… ${Math.round(overall * 100)}% · ${elapsed(job.started_at)}` : 'Run analysis'}
      </button>
      {running && (
        <div className="mt-2 h-1.5 overflow-hidden rounded bg-slate-100">
          <div className="h-full bg-sky-500 transition-all duration-500" style={{ width: `${Math.max(3, overall * 100)}%` }} />
        </div>
      )}
      {job && !running && job.finished_at && (
        <p className="mt-1 text-[0.75rem] text-slate-400">Last run took {elapsed(job.started_at, job.finished_at)}.</p>
      )}
      {message && <p className="mt-2 text-xs text-red-600">{message}</p>}

      {stages.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-1 text-xs font-semibold uppercase text-slate-500">Pipeline stages</h3>
          <ul className="space-y-1.5">
            {stages.map((s) => (
              <li key={s.stage} className="text-xs">
                <div className="flex min-w-0 items-center gap-2">
                  <span className={`w-16 shrink-0 rounded px-1.5 py-0.5 text-center font-medium ${STATUS_STYLES[s.status]}`}>
                    {s.status === 'running' && s.total ? `${s.done}/${s.total}` : s.status}
                  </span>
                  <span className="min-w-0 truncate" title={s.stage}>{s.stage}</span>
                </div>
                {s.note && s.status !== 'real' && <p className="mt-0.5 pl-[4.5rem] text-[0.75rem] text-slate-500">{s.note}</p>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!running && notes.length > 0 && (
        <details className="mt-4">
          <summary className="cursor-pointer text-xs font-semibold uppercase text-slate-500">Analysis notes ({notes.length})</summary>
          <ul className="mt-1 space-y-1">
            {notes.map(({ w, type }, i) => (
              <li key={i} className="flex gap-1.5 text-[0.75rem] text-slate-600"><SourceBadge type={type} /><span>{w}</span></li>
            ))}
          </ul>
        </details>
      )}
    </Panel>
  )
}
