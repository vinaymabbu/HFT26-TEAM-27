import { useState } from 'react'
import { API_BASE } from '../api'
import { BasisBadge, SourceBadge } from './Panel'

const MAX_SHOTS = 3

const frameHref = (img, frame) => `${API_BASE}/api/evidence/${img.evidence_id}/frames/${frame}?width=${img.width}`
const frameAt = (img, t) => Math.max(0, Math.round((t - (img.t_offset_s ?? 0)) * (img.fps ?? 1)))

// Where a vehicle track is at time t (linear between its points), or null outside its span.
function trackAt(track, t) {
  if (t < track[0].t_s - 0.3 || t > track.at(-1).t_s + 0.3) return null
  for (let i = 1; i < track.length; i++) {
    if (t <= track[i].t_s) {
      const a = track[i - 1]
      const b = track[i]
      const k = b.t_s === a.t_s ? 0 : Math.min(1, Math.max(0, (t - a.t_s) / (b.t_s - a.t_s)))
      return { x: a.x_m + (b.x_m - a.x_m) * k, y: a.y_m + (b.y_m - a.y_m) * k }
    }
  }
  return { x: track.at(-1).x_m, y: track.at(-1).y_m }
}

// The frames an event can show: a vehicle box the vision model drew, a tracked path, or the camera view a
// sensor spike came from. Each carries what the annotation needs and how it was obtained.
function shotsFor(event, observationsById, ea) {
  const img = ea?.scene?.image
  const out = []
  for (const id of event.observation_ids) {
    const o = observationsById[id]
    if (!o || out.length >= MAX_SHOTS) continue
    const a = o.attributes ?? {}
    const view = a.view && img?.views?.[a.view - 1] ? img.views[a.view - 1] : null
    if (o.source_type === 'video' && o.source_ref.evidence_id && !o.source_ref.evidence_id.startsWith('ev_fx_')) {
      const ref = { evidence_id: o.source_ref.evidence_id, fps: img?.fps, t_offset_s: img?.t_offset_s, width: a.frame_size?.[0] ?? img?.width }
      const fm = /^frame:(\d+)$/.exec(o.source_ref.locator)
      const [W, H] = a.frame_size ?? (img ? [img.width, img.height] : [])
      if (Array.isArray(a.box) && W && fm) {
        const [x1, y1, x2, y2] = a.box
        out.push({ kind: 'box', o, img: ref, frame: Number(fm[1]), W, H, view,
          box: [x1 / 1000 * W, y1 / 1000 * H, x2 / 1000 * W, y2 / 1000 * H] })
      } else if (o.action === 'track' && Array.isArray(a.points) && a.points.length > 1 && img?.fps) {
        out.push({ kind: 'track', o, img: ref, frame: frameAt(ref, o.t_rel_s), W: img.width, H: img.height, view: a.view_box ?? view })
      }
    } else if (o.source_type === 'sensor' && view && img && !img.evidence_id.startsWith('ev_fx_') && o.t_rel_s != null) {
      out.push({ kind: 'view', o, img, frame: frameAt(img, o.t_rel_s), W: img.width, H: img.height, view })
    }
  }
  return out
}

function Tag({ x, y, size, text, color }) {
  const w = text.length * size * 0.56 + size * 0.8
  return (
    <g>
      <rect x={x} y={y - size * 1.5} width={w} height={size * 1.5} fill={color} rx={size * 0.2} />
      <text x={x + size * 0.4} y={y - size * 0.4} fontSize={size} fontWeight="600" fill="#fff">{text}</text>
    </g>
  )
}

function Shot({ shot, ea }) {
  const [failed, setFailed] = useState(false)
  const { o, img, W, H } = shot
  const view = shot.view ?? [0, 0, W, H]
  const vw = view[2] - view[0]
  const vh = view[3] - view[1]
  const fs = vw / 38
  const stroke = vw / 260
  const conf = `${Math.round(o.confidence * 100)}%`
  const label = shot.kind === 'box'
    ? `${o.attributes.label ?? o.vehicle_description ?? 'vehicle'}${o.action ? ` · ${o.action.replaceAll('_', ' ')}` : ''} · ${conf}`
    : null
  let note
  let overlay = null
  let zoom = null

  if (shot.kind === 'box') {
    const [x1, y1, x2, y2] = shot.box
    const color = '#ef4444'
    overlay = (
      <g>
        <rect x={x1} y={y1} width={x2 - x1} height={y2 - y1} fill="none" stroke={color} strokeWidth={stroke * 1.6} />
        <Tag x={x1} y={Math.max(y1, view[1] + fs * 1.6)} size={fs} text={label} color={color} />
      </g>
    )
    const pad = Math.max(x2 - x1, y2 - y1) * 0.4
    zoom = [Math.max(view[0], x1 - pad), Math.max(view[1], y1 - pad), Math.min(view[2], x2 + pad), Math.min(view[3], y2 + pad)]
    note = 'The box is the AI vision model’s reading of where the vehicle is, not a measurement.'
  } else if (shot.kind === 'track') {
    const pts = o.attributes.points
    const here = pts.reduce((best, p) => (Math.abs(p[0] - o.t_rel_s) < Math.abs(best[0] - o.t_rel_s) ? p : best), pts[0])
    const color = '#0ea5e9'
    overlay = (
      <g>
        <polyline points={pts.map((p) => `${p[1]},${p[2]}`).join(' ')} fill="none" stroke={color} strokeWidth={stroke * 1.8} />
        <circle cx={pts[0][1]} cy={pts[0][2]} r={stroke * 3} fill="#fff" stroke={color} strokeWidth={stroke} />
        <circle cx={here[1]} cy={here[2]} r={stroke * 4} fill={color} stroke="#fff" strokeWidth={stroke} />
        <Tag x={here[1] + stroke * 6} y={here[2]} size={fs} text={`${o.attributes.track_id} · ${o.attributes.direction ?? 'moving'}`} color={color} />
      </g>
    )
    note = 'Path of one tracked vehicle through this camera view (motion tracking, image pixels). Circle = where it is at the event time.'
  } else {
    const t = o.t_rel_s
    const here = ea.vehicles.filter((v) => Number(v.vehicle_id.slice(1).split('-')[0]) === o.attributes.view)
      .map((v) => ({ v, p: trackAt(v.track, t) })).filter((e) => e.p)
    overlay = (
      <g>
        <rect x={view[0] + stroke} y={view[1] + stroke} width={vw - 2 * stroke} height={vh - 2 * stroke} fill="none" stroke="#f59e0b" strokeWidth={stroke * 2} />
        {here.map(({ v, p }) => (
          <g key={v.vehicle_id}>
            <circle cx={p.x} cy={p.y} r={stroke * 3.5} fill={v.color} stroke="#fff" strokeWidth={stroke} />
            <text x={p.x + stroke * 5} y={p.y - stroke * 3} fontSize={fs * 0.8} fontWeight="700" fill="#fff" stroke="#000" strokeWidth={stroke * 0.8} paintOrder="stroke">{v.vehicle_id}</text>
          </g>
        ))}
        <Tag x={view[0] + stroke * 4} y={view[1] + fs * 2} size={fs} text={`motion spike · view ${o.attributes.view}`} color="#d97706" />
      </g>
    )
    note = `A sensor spike does not say which vehicle caused it. ${here.length ? `Dots are the vehicles the tracker had in this view at t=${t}s.` : `The tracker had no vehicle in this view at t=${t}s.`}`
  }

  const frame = frameHref(img, shot.frame)
  if (failed) {
    return (
      <div className="rounded border border-dashed border-slate-300 p-3 text-xs text-slate-500">
        The video frame for this event ({shot.frame}) could not be loaded. Is the video still uploaded? The evidence list below still applies.
      </div>
    )
  }
  return (
    <figure className="min-w-0 overflow-hidden rounded border border-slate-200 bg-white">
      <div className={zoom ? 'grid gap-px bg-slate-200 sm:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]' : ''}>
        <a href={frame.replace(/width=\d+/, 'width=1280')} target="_blank" rel="noreferrer" className="block bg-black">
          <svg viewBox={`${view[0]} ${view[1]} ${vw} ${vh}`} className="block w-full" role="img" aria-label={`annotated frame ${shot.frame}`}>
            <image href={frame} x={0} y={0} width={W} height={H} onError={() => setFailed(true)} />
            {overlay}
          </svg>
        </a>
        {zoom && (
          <div className="relative aspect-[4/3] bg-black sm:aspect-auto">
            <svg viewBox={`${zoom[0]} ${zoom[1]} ${zoom[2] - zoom[0]} ${zoom[3] - zoom[1]}`} className="absolute inset-0 h-full w-full" preserveAspectRatio="xMidYMid slice" role="img" aria-label="zoom on the vehicle">
              <image href={frame} x={0} y={0} width={W} height={H} />
              <rect x={shot.box[0]} y={shot.box[1]} width={shot.box[2] - shot.box[0]} height={shot.box[3] - shot.box[1]} fill="none" stroke="#ef4444" strokeWidth={(zoom[2] - zoom[0]) / 120} />
            </svg>
          </div>
        )}
      </div>
      <figcaption className="space-y-1 p-2 text-[0.75rem] text-slate-600">
        <div className="flex flex-wrap items-center gap-1.5">
          <SourceBadge type={o.source_type} /><BasisBadge basis={o.basis} />
          <span className="font-medium text-slate-700">{o.vehicle_description ?? o.action.replaceAll('_', ' ')}</span>
          <span className="text-slate-400">frame {shot.frame} · t={o.t_rel_s}s · conf {conf}</span>
        </div>
        <p className="text-slate-500">{note}</p>
      </figcaption>
    </figure>
  )
}

// Annotated video frames for the selected timeline event: what the evidence behind it actually looked like.
export default function EventFrame({ event, observationsById, eventAnalysis }) {
  const shots = shotsFor(event, observationsById, eventAnalysis)
  if (shots.length === 0) {
    const kinds = [...new Set(event.source_types)].join(' and ')
    return (
      <p className="mb-3 rounded border border-dashed border-slate-300 p-2 text-xs text-slate-500">
        No video frame for this event: it rests on {kinds} evidence only, so there is no image of a vehicle to annotate.
      </p>
    )
  }
  return (
    <div className="mb-3 grid gap-3 lg:grid-cols-2 [&>*:only-child]:lg:col-span-2">
      {shots.map((s) => <Shot key={s.o.id} shot={s} ea={eventAnalysis} />)}
    </div>
  )
}
