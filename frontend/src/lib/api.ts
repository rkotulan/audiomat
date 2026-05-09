// Tiny fetch wrapper for the audiomat FastAPI backend.
// Vite proxies /api → :8000 in dev, so we can use relative paths everywhere.
import type {
  DraftUploadResult,
  Project,
  ProgressEvent,
  Voice,
} from './types'

const BASE = '/api'

async function ok<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const detail = await r.text().catch(() => r.statusText)
    throw new Error(`${r.status} ${r.statusText}: ${detail}`)
  }
  return r.json() as Promise<T>
}

// ---- voices ----

export const listVoices = () =>
  fetch(`${BASE}/voices`).then(ok<Voice[]>)

export const getVoice = (slug: string) =>
  fetch(`${BASE}/voices/${slug}`).then(ok<Voice>)

export async function draftUploadVoice(file: File): Promise<DraftUploadResult> {
  const fd = new FormData()
  fd.append('audio', file)
  return fetch(`${BASE}/voices/draft-upload`, { method: 'POST', body: fd }).then(
    ok<DraftUploadResult>,
  )
}

export async function autoTranscribe(audio_path: string, language = 'cs') {
  return fetch(`${BASE}/voices/auto-transcribe`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ audio_path, language }),
  }).then(ok<{ transcript: string }>)
}

export async function createVoice(args: {
  name: string
  audio_path: string
  transcript: string
  notes?: string
  overwrite?: boolean
}): Promise<Voice> {
  const fd = new FormData()
  fd.append('name', args.name)
  fd.append('audio_path', args.audio_path)
  fd.append('transcript', args.transcript)
  fd.append('notes', args.notes ?? '')
  fd.append('overwrite', args.overwrite ? 'true' : 'false')
  return fetch(`${BASE}/voices`, { method: 'POST', body: fd }).then(ok<Voice>)
}

export const deleteVoice = (slug: string) =>
  fetch(`${BASE}/voices/${slug}`, { method: 'DELETE' }).then(ok)

export const voiceAudioUrl = (slug: string) => `${BASE}/voices/${slug}/audio`

// ---- projects ----

export const listProjects = () =>
  fetch(`${BASE}/projects`).then(ok<Project[]>)

export const getProject = (slug: string) =>
  fetch(`${BASE}/projects/${slug}`).then(ok<Project>)

export async function createProject(args: {
  name: string
  voice_ref: string
  book: File
  overwrite?: boolean
}): Promise<Project> {
  const fd = new FormData()
  fd.append('name', args.name)
  fd.append('voice_ref', args.voice_ref)
  fd.append('book', args.book)
  fd.append('overwrite', args.overwrite ? 'true' : 'false')
  return fetch(`${BASE}/projects`, { method: 'POST', body: fd }).then(ok<Project>)
}

export const updateProjectParams = (slug: string, params: Partial<Project['params']>) =>
  fetch(`${BASE}/projects/${slug}/params`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  }).then(ok<Project>)

export const deleteProject = (slug: string) =>
  fetch(`${BASE}/projects/${slug}`, { method: 'DELETE' }).then(ok)

export const startRender = (slug: string) =>
  fetch(`${BASE}/projects/${slug}/render`, { method: 'POST' }).then(ok)

export const buildM4b = (slug: string) =>
  fetch(`${BASE}/projects/${slug}/build-m4b`, { method: 'POST' }).then(ok)

export const projectM4bUrl = (slug: string) => `${BASE}/projects/${slug}/m4b`

// ---- SSE progress stream ----

export function subscribeProgress(
  slug: string,
  onEvent: (e: ProgressEvent) => void,
  onError?: (err: Event) => void,
): () => void {
  const es = new EventSource(`${BASE}/projects/${slug}/progress`)
  // Backend tags each event by .kind; addEventListener by kind so we catch all.
  const kinds: ProgressEvent['kind'][] = [
    'render_start',
    'chunk_synthed',
    'chunk_cached',
    'chapter_concat_start',
    'chapter_done',
    'chapter_skipped',
    'render_complete',
    'error',
  ]
  for (const k of kinds) {
    es.addEventListener(k, (msg) => {
      try {
        onEvent(JSON.parse((msg as MessageEvent).data) as ProgressEvent)
      } catch {
        // ignore malformed events
      }
    })
  }
  if (onError) es.onerror = onError
  return () => es.close()
}
