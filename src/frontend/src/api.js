let direct = null
async function directUploadAvailable() {
  if (direct === null) direct = await request('/health').then((h) => !!h.direct_upload).catch(() => false)
  return direct
}

// Thin wrapper over the FastAPI endpoints. Shapes are defined in
// src/backend/app/schemas.py (the integration contract).
// Empty in development (Vite proxies /api). On Vercel set VITE_API_BASE to the backend's address, e.g. https://xyz.onrender.com
export const API_BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

async function request(path, options) {
  const res = await fetch(`${API_BASE}/api${path}`, options)
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `${res.status} ${res.statusText}`)
  }
  return res.status === 204 ? null : res.json()
}

export const api = {
  listEvidence: () => request('/evidence'),
  // Each kind has its own endpoint with its own validation (video / witness / sensor).
  health: () => request('/health'),
  uploadEvidence: async (file) => {
    const ext = file.name.split('.').pop().toLowerCase()
    const kind = ext === 'txt' ? 'witness' : ext === 'csv' ? 'sensor' : 'video'
    // Hosted on a serverless platform a request body is capped at a few MB: send videos straight to the bucket and
    // register them. Everything else (and local development) uses the normal upload.
    if (kind === 'video' && file.size > 4 * 1024 * 1024 && (await directUploadAvailable())) {
      const slot = await request('/evidence/presign', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ filename: file.name, size_bytes: file.size }),
      })
      const put = await fetch(slot.url, { method: slot.method, body: file })
      if (!put.ok) throw new Error(`Upload to storage failed (${put.status}). Is the bucket's CORS set to allow PUT from this site?`)
      return request('/evidence/register', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ key: slot.key, filename: file.name }),
      })
    }
    const form = new FormData()
    form.append('file', file)
    return request(`/evidence/${kind}`, { method: 'POST', body: form })
  },
  deleteEvidence: (id) => request(`/evidence/${id}`, { method: 'DELETE' }),
  runAnalysis: () => request('/analysis/run', { method: 'POST' }),
  startJob: () => request('/analysis/jobs', { method: 'POST' }),
  currentJob: () => request('/analysis/jobs/current'),
  latestRun: () => request('/analysis/latest'),
  observations: () => request('/observations'),
  timeline: () => request('/timeline'),
  conflicts: () => request('/conflicts'),
  reconstruction: () => request('/reconstruction'),
  positionsUrl: `${API_BASE}/api/reconstruction/positions?download=true`,
  eventAnalysis: () => request('/event-analysis'),
}

// Thumbnail URL for a `frame:<n>` source ref of an uploaded video, or null.
export function frameUrl(observation, width = 320) {
  const m = /^frame:(\d+)$/.exec(observation?.source_ref?.locator ?? '')
  if (!m || observation.source_type !== 'video' || observation.source_ref.evidence_id.startsWith('ev_fx_')) return null
  return `${API_BASE}/api/evidence/${observation.source_ref.evidence_id}/frames/${m[1]}?width=${width}`
}
