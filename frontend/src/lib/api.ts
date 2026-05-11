// Tiny fetch wrapper for the audiomat FastAPI backend.
// Vite proxies /api → :8000 in dev, so we can use relative paths everywhere.
import type {
  ChapterText,
  ChaptersResponse,
  CustomPreviewResult,
  DraftUploadResult,
  HFRepoInfo,
  HFTokenStatus,
  ModelDownloadEvent,
  PreviewMatrix,
  Project,
  ProgressEvent,
  TTSModel,
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

export const draftAudioUrl = (path: string) =>
  `${BASE}/voices/draft-audio?path=${encodeURIComponent(path)}`

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
  tts_model?: string | null
}): Promise<Voice> {
  const fd = new FormData()
  fd.append('name', args.name)
  fd.append('audio_path', args.audio_path)
  fd.append('transcript', args.transcript)
  fd.append('notes', args.notes ?? '')
  fd.append('overwrite', args.overwrite ? 'true' : 'false')
  if (args.tts_model) fd.append('tts_model', args.tts_model)
  return fetch(`${BASE}/voices`, { method: 'POST', body: fd }).then(ok<Voice>)
}

export const updateVoiceModel = (slug: string, tts_model: string | null) =>
  fetch(`${BASE}/voices/${slug}/model`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tts_model }),
  }).then(ok<Voice>)

export const deleteVoice = (slug: string) =>
  fetch(`${BASE}/voices/${slug}`, { method: 'DELETE' }).then(ok)

export const voiceAudioUrl = (slug: string) => `${BASE}/voices/${slug}/audio`

// ---- TTS model registry ----

export const listModels = (): Promise<TTSModel[]> =>
  fetch(`${BASE}/models`).then(ok<TTSModel[]>)

export const getModel = (slug: string): Promise<TTSModel> =>
  fetch(`${BASE}/models/${slug}`).then(ok<TTSModel>)

export const registerLocalModel = (body: {
  name: string
  src_dir: string
  notes?: string
  overwrite?: boolean
}): Promise<TTSModel> =>
  fetch(`${BASE}/models`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(ok<TTSModel>)

export const deleteModel = (slug: string) =>
  fetch(`${BASE}/models/${slug}`, { method: 'DELETE' }).then(ok)

export const listMyHFRepos = (): Promise<HFRepoInfo[]> =>
  fetch(`${BASE}/models/hf/my-repos`).then(ok<HFRepoInfo[]>)

export interface ModelDownloadEvents {
  onStarted?: (totalBytes: number) => void
  onProgress?: (downloadedBytes: number, totalBytes: number, percent: number) => void
}

async function consumeModelDownloadSse(
  r: Response,
  events: ModelDownloadEvents,
): Promise<{ model_slug: string }> {
  if (!r.ok) {
    const detail = await r.text().catch(() => r.statusText)
    throw new Error(`${r.status} ${r.statusText}: ${detail}`)
  }
  if (!r.body) throw new Error('model-download: no response body')

  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    let sep
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      const evt = parseSSE(block)
      if (!evt) continue
      const data = evt.data as ModelDownloadEvent['data' & never] as ModelDownloadEvent
      if (evt.event === 'started') {
        events.onStarted?.(data.total_bytes ?? 0)
      } else if (evt.event === 'progress') {
        events.onProgress?.(
          data.downloaded_bytes ?? 0,
          data.total_bytes ?? 0,
          data.percent ?? 0,
        )
      } else if (evt.event === 'error') {
        throw new Error(data.message ?? 'download failed')
      } else if (evt.event === 'complete') {
        return { model_slug: data.model_slug ?? '' }
      }
    }
  }
  throw new Error('model-download: stream ended without complete event')
}

export async function downloadHFModel(
  body: {
    name: string
    repo_id: string
    revision?: string | null
    token?: string | null
    notes?: string
    overwrite?: boolean
  },
  events: ModelDownloadEvents = {},
): Promise<{ model_slug: string }> {
  const r = await fetch(`${BASE}/models/from-hf`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return consumeModelDownloadSse(r, events)
}

export async function redownloadModel(
  slug: string,
  events: ModelDownloadEvents = {},
): Promise<{ model_slug: string }> {
  const r = await fetch(`${BASE}/models/${slug}/redownload`, { method: 'POST' })
  return consumeModelDownloadSse(r, events)
}

// ---- Settings ----

export const getHFTokenStatus = (): Promise<HFTokenStatus> =>
  fetch(`${BASE}/settings/hf`).then(ok<HFTokenStatus>)

export const setHFToken = (token: string | null): Promise<HFTokenStatus> =>
  fetch(`${BASE}/settings/hf`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  }).then(ok<HFTokenStatus>)

export const clearHFToken = (): Promise<HFTokenStatus> =>
  fetch(`${BASE}/settings/hf`, { method: 'DELETE' }).then(ok<HFTokenStatus>)

export const validateHFToken = (token: string): Promise<{ valid: boolean; repo_count: number }> =>
  fetch(`${BASE}/settings/hf/validate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  }).then(ok<{ valid: boolean; repo_count: number }>)

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
  language?: string
}): Promise<Project> {
  const fd = new FormData()
  fd.append('name', args.name)
  fd.append('voice_ref', args.voice_ref)
  fd.append('book', args.book)
  fd.append('overwrite', args.overwrite ? 'true' : 'false')
  if (args.language) fd.append('language', args.language)
  return fetch(`${BASE}/projects`, { method: 'POST', body: fd }).then(ok<Project>)
}

export const updateProjectParams = (slug: string, params: Partial<Project['params']>) =>
  fetch(`${BASE}/projects/${slug}/params`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  }).then(ok<Project>)

export const updateProjectBook = (slug: string, body: { language?: string }) =>
  fetch(`${BASE}/projects/${slug}/book`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(ok<Project>)

export const updateBlocksSkipped = (slug: string, indices: number[]) =>
  fetch(`${BASE}/projects/${slug}/blocks-skipped`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ indices }),
  }).then(ok<Project>)

export const deleteProject = (slug: string) =>
  fetch(`${BASE}/projects/${slug}`, { method: 'DELETE' }).then(ok)

export interface PreviewMatrixEvents {
  onStarted?: (header: Omit<PreviewMatrix, 'variants'> & { total: number }) => void
  onCellDone?: (index: number, variant: PreviewMatrix['variants'][number]) => void
  onError?: (message: string) => void
}

/** Stream the 4-cell preview matrix. Backend yields SSE events as each
 *  cell finishes; we surface them so the UI can show "X / 4 done"
 *  progress instead of a hung spinner. Resolves with the full matrix
 *  on the ``complete`` event. */
export async function previewMatrix(
  slug: string,
  events: PreviewMatrixEvents = {},
): Promise<PreviewMatrix> {
  const r = await fetch(`${BASE}/projects/${slug}/preview-matrix`, {
    method: 'POST',
  })
  if (!r.ok) {
    const detail = await r.text().catch(() => r.statusText)
    throw new Error(`${r.status} ${r.statusText}: ${detail}`)
  }
  if (!r.body) throw new Error('preview-matrix: no response body')

  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let header: (Omit<PreviewMatrix, 'variants'> & { total: number }) | null = null
  const variants: PreviewMatrix['variants'][number][] = []

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    // sse-starlette/uvicorn emits CRLF line endings (\r\n\r\n between
    // events); normalize so the LF-only split below works regardless.
    buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    let sep
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      const evt = parseSSE(block)
      if (!evt) continue
      if (evt.event === 'started') {
        header = evt.data
        events.onStarted?.(evt.data)
      } else if (evt.event === 'cell_done') {
        variants[evt.data.index] = evt.data.variant
        events.onCellDone?.(evt.data.index, evt.data.variant)
      } else if (evt.event === 'error') {
        const msg = evt.data?.message ?? 'unknown error'
        events.onError?.(msg)
        throw new Error(msg)
      } else if (evt.event === 'complete') {
        // header is guaranteed by the server contract (started fires first)
        if (!header) throw new Error('preview-matrix: complete without started')
        return {
          sample_text: header.sample_text,
          sample_chars: header.sample_chars,
          sample_block_index: header.sample_block_index,
          sample_block_total: header.sample_block_total,
          total_book_chars: header.total_book_chars,
          variants: evt.data.variants,
        }
      }
    }
  }
  throw new Error('preview-matrix: stream ended without complete event')
}

function parseSSE(block: string): { event: string; data: any } | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }
  if (dataLines.length === 0) return null
  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null
  }
}

export const previewCustom = (
  slug: string,
  params: {
    num_step: number
    guidance_scale: number
    speed: number
    // Optional matrix-cell label. When present, the backend stores this
    // tuning in previews/_tuned_cells.json so the matrix shows it on
    // next render too (survives page refresh).
    label?: string
  },
): Promise<CustomPreviewResult> =>
  fetch(`${BASE}/projects/${slug}/preview-custom`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(params),
  }).then(ok<CustomPreviewResult>)

export const listChapters = (slug: string): Promise<ChaptersResponse> =>
  fetch(`${BASE}/projects/${slug}/chapters`).then(ok<ChaptersResponse>)

export const chapterAudioUrl = (slug: string, stem: string) =>
  `${BASE}/projects/${slug}/chapter-audio/${encodeURIComponent(stem)}`

export const resetChapter = (slug: string, stem: string) =>
  fetch(`${BASE}/projects/${slug}/chapters/${encodeURIComponent(stem)}`, {
    method: 'DELETE',
  }).then(ok)

export const resetAllChapters = (slug: string) =>
  fetch(`${BASE}/projects/${slug}/chapters`, { method: 'DELETE' }).then(
    ok<{ reset_count: number }>,
  )

// ---- per-chapter text override ----

export const getChapterText = (slug: string, stem: string): Promise<ChapterText> =>
  fetch(
    `${BASE}/projects/${slug}/chapters/${encodeURIComponent(stem)}/text`,
  ).then(ok<ChapterText>)

export const saveChapterText = (
  slug: string,
  stem: string,
  text: string,
): Promise<ChapterText> =>
  fetch(`${BASE}/projects/${slug}/chapters/${encodeURIComponent(stem)}/text`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  }).then(ok<ChapterText>)

export const resetChapterText = (slug: string, stem: string): Promise<ChapterText> =>
  fetch(`${BASE}/projects/${slug}/chapters/${encodeURIComponent(stem)}/text`, {
    method: 'DELETE',
  }).then(ok<ChapterText>)

// ---- per-project pronunciation dictionary ----

export const getPronunciations = (slug: string): Promise<Record<string, string>> =>
  fetch(`${BASE}/projects/${slug}/pronunciations`).then(ok<Record<string, string>>)

export const savePronunciations = (
  slug: string,
  mapping: Record<string, string>,
): Promise<Record<string, string>> =>
  fetch(`${BASE}/projects/${slug}/pronunciations`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(mapping),
  }).then(ok<Record<string, string>>)

export const startRender = (slug: string, indices?: number[]) =>
  fetch(`${BASE}/projects/${slug}/render`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ indices: indices ?? null }),
  }).then(ok)

export const cancelRender = (slug: string) =>
  fetch(`${BASE}/projects/${slug}/cancel-render`, { method: 'POST' }).then(ok)

export interface BuildM4bStarted {
  chapters: number
  duration_s: number
}

export interface BuildM4bComplete {
  chapters: number
  duration_s: number
  size_bytes: number
}

export interface BuildM4bEvents {
  onStarted?: (info: BuildM4bStarted) => void
  onProgress?: (percent: number) => void
}

/** Stream M4B build progress via SSE so the user gets a live encoder
 *  percent instead of a hung spinner. Resolves with the complete
 *  payload (chapters + duration + final file size). */
export async function buildM4b(
  slug: string,
  events: BuildM4bEvents = {},
): Promise<BuildM4bComplete> {
  const r = await fetch(`${BASE}/projects/${slug}/build-m4b`, { method: 'POST' })
  if (!r.ok) {
    const detail = await r.text().catch(() => r.statusText)
    throw new Error(`${r.status} ${r.statusText}: ${detail}`)
  }
  if (!r.body) throw new Error('build-m4b: no response body')

  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    let sep
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      const evt = parseSSE(block)
      if (!evt) continue
      if (evt.event === 'started') {
        events.onStarted?.(evt.data)
      } else if (evt.event === 'progress') {
        events.onProgress?.(evt.data.percent)
      } else if (evt.event === 'error') {
        throw new Error(evt.data?.message ?? 'M4B build failed')
      } else if (evt.event === 'complete') {
        return evt.data as BuildM4bComplete
      }
    }
  }
  throw new Error('build-m4b: stream ended without complete event')
}

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
