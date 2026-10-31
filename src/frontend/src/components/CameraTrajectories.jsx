import { useEffect, useRef, useState } from 'react'
import { API_BASE } from '../api'

// Position of a pixel track at time t (linear between points), or null outside its span.
function at(track, t) {
  if (t < track[0].t_s || t > track[track.length - 1].t_s) return null
  for (let i = 1; i < track.length; i++) {
    const a = track[i - 1]
    const b = track[i]
    if (t <= b.t_s) {
      const k = b.t_s === a.t_s ? 0 : (t - a.t_s) / (b.t_s - a.t_s)
      const speed = a.speed_kmh != null && b.speed_kmh != null ? a.speed_kmh + (b.speed_kmh - a.speed_kmh) * k : null
      const box = a.box && b.box ? a.box.map((v, j) => v + (b.box[j] - v) * k) : null
      return { x: a.x_m + (b.x_m - a.x_m) * k, y: a.y_m + (b.y_m - a.y_m) * k, speed, box }
    }
  }
  const last = track[track.length - 1]
  return { x: last.x_m, y: last.y_m, speed: last.speed_kmh, box: last.box ?? null }
}

// Tracks from the vision model's boxes (not from pixel motion) are inferred: drawn dotted and named.
const isAi = (v) => (v.description ?? '').startsWith('AI-sighted')

// Fixed-camera reconstruction: tracks drawn on the actual video frame, in image pixels.
export default function CameraTrajectories({ data, time, onTimeChange, Panel, title = 'Event Analysis' }) {
  const [playing, setPlaying] = useState(false)
  const [showAll, setShowAll] = useState(true)
  const [showBoxes, setShowBoxes] = useState(true)
  const [view, setView] = useState(0) // 0 = all views
  // Set when a frame fails to load (the video was deleted or replaced since this run): say so, don't draw over black.
  const [frameMissing, setFrameMissing] = useState(false)
  const timeRef = useRef(time)
  useEffect(() => { timeRef.current = time }, [time])

  useEffect(() => {
    if (!playing) return
    let frame
    let last = performance.now()
    const tick = (now) => {
      const next = timeRef.current + (now - last) / 1000
      last = now
      if (next >= data.t_max_s) {
        onTimeChange(data.t_max_s)
        setPlaying(false)
        return
      }
      onTimeChange(next)
      frame = requestAnimationFrame(tick)
    }
    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [playing, data, onTimeChange])

  const img = data.scene.image
  const views = img.views ?? []
  const box = view && views[view - 1] ? views[view - 1] : [0, 0, img.width, img.height]
  const t = Math.min(Math.max(time, data.t_min_s), data.t_max_s)
  // Track ids are T<view>-<n> (moving) and S<view>-<n> (stopped).
  const inView = (v) => !view || Number(v.vehicle_id.slice(1).split('-')[0]) === view
  const vehicles = data.vehicles.filter(inView)
  const active = vehicles.filter((v) => at(v.track, t))
  const stopped = vehicles.filter((v) => v.vehicle_id.startsWith('S'))
  useEffect(() => { setFrameMissing(false) }, [img.evidence_id])
  const r = Math.max(3, (box[2] - box[0]) / 120)
  // Replay: the background is the video frame at the current time, in 0.5 s steps (cached by the browser).
  const frame = img.fps
    ? Math.max(0, Math.round((Math.round(t * 2) / 2 - (img.t_offset_s ?? 0)) * img.fps))
    : img.frame

  const togglePlay = () => {
    if (!playing && time >= data.t_max_s) onTimeChange(data.t_min_s)
    setPlaying(!playing)
  }

  return (
    <Panel title={title}
      actions={
        <select value={view} onChange={(e) => setView(Number(e.target.value))} className="rounded border border-slate-200 text-xs">
          <option value={0}>All views</option>
          {views.map((_, i) => <option key={i} value={i + 1}>View {i + 1}</option>)}
        </select>
      }>
      <div className="relative mx-auto w-full overflow-hidden rounded bg-black" style={{ aspectRatio: `${box[2] - box[0]} / ${box[3] - box[1]}` }}>
        <svg viewBox={`${box[0]} ${box[1]} ${box[2] - box[0]} ${box[3] - box[1]}`} className="absolute inset-0 h-full w-full">
          <image href={`${API_BASE}/api/evidence/${img.evidence_id}/frames/${frame}?width=${img.width}`}
            x={0} y={0} width={img.width} height={img.height} opacity={frameMissing ? 0 : 0.85}
            onLoad={() => setFrameMissing(false)} onError={() => setFrameMissing(true)} />
          {vehicles.map((v) => {
            const past = v.track.filter((p) => p.t_s <= t)
            const pts = (showAll ? v.track : past).map((p) => `${p.x_m},${p.y_m}`).join(' ')
            return pts && <polyline key={v.vehicle_id} points={pts} fill="none" stroke={v.color}
              strokeWidth={r / 2.5} strokeOpacity={showAll ? 0.35 : 0.8}
              {...(isAi(v) ? { strokeLinecap: 'round', strokeDasharray: `0 ${r * 1.1}`, strokeWidth: r / 1.6, strokeOpacity: showAll ? 0.55 : 0.9 } : {})} />
          })}
          {showBoxes && active.map((v) => {
            // Detection box around the vehicle: solid where the motion tracker found it, dashed where it is the AI model's reading.
            const b = at(v.track, t)?.box
            return b && <rect key={`box-${v.vehicle_id}`} x={b[0]} y={b[1]} width={b[2] - b[0]} height={b[3] - b[1]}
              fill="none" stroke={v.color} strokeWidth={r / 2.2} {...(isAi(v) ? { strokeDasharray: `${r * 1.4} ${r}` } : {})}>
              <title>{`${v.label}: ${isAi(v) ? "AI model's box (inferred)" : 'motion-tracker box'}`}</title></rect>
          })}
          {active.map((v) => {
            const p = at(v.track, t)
            const isStop = v.vehicle_id.startsWith('S')
            return isStop
              ? <rect key={v.vehicle_id} x={p.x - r * 1.6} y={p.y - r * 1.6} width={r * 3.2} height={r * 3.2}
                  fill="none" stroke={v.color} strokeWidth={r / 2}><title>{v.label}</title></rect>
              : <g key={v.vehicle_id}>
                  <circle cx={p.x} cy={p.y} r={r} fill={isAi(v) ? 'none' : v.color} stroke={isAi(v) ? v.color : 'white'} strokeWidth={r / (isAi(v) ? 2.2 : 4)}>
                    <title>{`${v.label}: ${v.description}`}</title></circle>
                  {isAi(v) && <text x={p.x + r * 1.4} y={p.y + r * 0.4} fontSize={r * 2.4} fontWeight="700" fill="white"
                    stroke="black" strokeWidth={r / 3} paintOrder="stroke">{v.label}</text>}
                  {p.speed != null && <text x={p.x + r * 1.4} y={p.y - r} fontSize={r * 2.6} fontWeight="700" fill="white"
                    stroke="black" strokeWidth={r / 3} paintOrder="stroke">{Math.round(p.speed)} km/h</text>}
                </g>
          })}
        </svg>
        {frameMissing && (
          <div role="alert" className="absolute inset-x-0 top-0 bg-amber-50/95 px-3 py-2 text-xs text-amber-900">
            <b>Footage unavailable.</b> Video <span className="font-mono">{img.evidence_id}</span> is no longer in the
            evidence list (or its frames cannot be read). Tracks are shown without footage. Re-run the analysis.
          </div>
        )}
      </div>

      <div className="mt-3 flex items-center gap-3">
        <button onClick={togglePlay} className="w-16 rounded bg-slate-900 px-2 py-1 text-xs font-medium text-white">
          {playing ? 'Pause' : 'Play'}
        </button>
        <input type="range" min={data.t_min_s} max={data.t_max_s} step={0.1} value={t}
          onChange={(e) => { setPlaying(false); onTimeChange(Number(e.target.value)) }} className="min-w-0 flex-1" />
        <span className="w-16 text-right font-mono text-xs">t={t.toFixed(1)}s</span>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-600">
        <span>{vehicles.length - stopped.length} moving tracks · {active.length} on screen now</span>
        {stopped.length > 0 && <span className="text-red-600">■ {stopped.map((v) => v.label).join(', ')}</span>}
        <label className="flex items-center gap-1">
          <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> full paths
        </label>
        <label className="flex items-center gap-1" title="Solid: motion tracker. Dashed: the AI model's box (inferred).">
          <input type="checkbox" checked={showBoxes} onChange={(e) => setShowBoxes(e.target.checked)} /> boxes
        </label>
        <span className="text-slate-400">
          {data.vehicles.some((v) => v.track.some((p) => p.speed_kmh != null))
            ? 'speeds from lane-marking calibration where a view is calibrated'
            : 'image pixels (no view calibrated)'}
        </span>
      </div>
    </Panel>
  )
}
