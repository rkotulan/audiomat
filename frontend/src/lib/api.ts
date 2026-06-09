// Tiny fetch wrapper for the audiomat FastAPI backend.
// Vite proxies /api → :8000 in dev, so we can use relative paths everywhere.
import type {
  AnalyzeResult,
  BackupSize,
  ChapterText,
  ChaptersResponse,
  CustomPreviewResult,
  DraftUploadLongResult,
  DraftUploadResult,
  HFRepoInfo,
  HFTokenStatus,
  ModelDownloadEvent,
  PreviewMatrix,
  PreviewVoicesMatrix,
  Project,
  ProgressEvent,
  RestoreResult,
  StagedVoicePreview,
  TTSModel,
  Voice,
  VoicePreviewCell,
} from './types'

const BASE = '/api'

async function ok<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const detail = await r.text().catch(() => r.statusText)
    throw new Error(`${r.status} ${r.statusText}: ${detail}`)
  }
  return r.json() as Promise<T>
}

/** Thrown by project PATCH helpers when the server returned 409
 *  (project was modified in another tab since the version we sent).
 *  The caller can catch this specific type to render a "refresh
 *  required" banner instead of a generic error. */
export class ProjectVersionConflict extends Error {
  expected: number
  current: number
  constructor(expected: number, current: number) {
    super(
      `project version mismatch: expected ${expected}, server has ${current}`,
    )
    this.name = 'ProjectVersionConflict'
    this.expected = expected
    this.current = current
  }
}

/** PATCH wrapper that adds ``If-Match`` and translates 409 into a
 *  typed :class:`ProjectVersionConflict`. All other failures bubble
 *  up as a regular Error matching the rest of the API client. */
async function patchWithVersion<T>(
  url: string,
  body: unknown,
  expectedVersion: number,
): Promise<T> {
  const r = await fetch(url, {
    method: 'PATCH',
    headers: {
      'Content-Type': 'application/json',
      'If-Match': String(expectedVersion),
    },
    body: JSON.stringify(body),
  })
  if (r.status === 409) {
    const data = await r.json().catch(() => ({}))
    const detail = data?.detail ?? {}
    throw new ProjectVersionConflict(
      detail.expected_version ?? expectedVersion,
      detail.current_version ?? expectedVersion,
    )
  }
  return ok<T>(r)
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

// ---- long-source voice picker (multi-step wizard) ----

export async function draftUploadVoiceLong(file: File): Promise<DraftUploadLongResult> {
  const fd = new FormData()
  fd.append('audio', file)
  return fetch(`${BASE}/voices/draft-upload-long`, { method: 'POST', body: fd }).then(
    ok<DraftUploadLongResult>,
  )
}

export async function analyzeVoiceSource(args: {
  audio_path: string
  chapter_start_s?: number | null
  chapter_end_s?: number | null
  analyze_minutes?: number
}): Promise<AnalyzeResult> {
  return fetch(`${BASE}/voices/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      audio_path: args.audio_path,
      chapter_start_s: args.chapter_start_s ?? null,
      chapter_end_s: args.chapter_end_s ?? null,
      analyze_minutes: args.analyze_minutes ?? 10,
    }),
  }).then(ok<AnalyzeResult>)
}

export async function previewStagedVoice(args: {
  audio_path: string
  transcript: string
  sample_text: string
  language?: string
  // v0.4: route through a registered TTS model (e.g. Higgs) instead of
  // stock OmniVoice. Null / undefined → stock OmniVoice (default).
  tts_model_slug?: string | null
}): Promise<StagedVoicePreview> {
  return fetch(`${BASE}/voices/preview-staged`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      audio_path: args.audio_path,
      transcript: args.transcript,
      sample_text: args.sample_text,
      language: args.language ?? 'cs',
      tts_model_slug: args.tts_model_slug ?? null,
    }),
  }).then(ok<StagedVoicePreview>)
}

export async function extractVoiceWindow(args: {
  audio_path: string
  analyzed_start_s: number
  start_s: number
  end_s: number
}): Promise<DraftUploadResult> {
  return fetch(`${BASE}/voices/extract-window`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(args),
  }).then(ok<DraftUploadResult>)
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

export interface DeleteVoiceResult {
  deleted: string
  replacement: string | null
  replaced_in: string[]
}

export interface VoiceInUseError {
  status: 409
  message: string
  referencing_projects: { slug: string; name: string }[]
}

/** Delete a voice. When ``replacement`` is provided the server atomically
 *  reassigns every referencing project to it before deletion.
 *
 *  When the voice is in use and no replacement is given, the server
 *  returns a structured 409 — we surface it as a typed
 *  ``VoiceInUseError`` so the UI can prompt the user to pick a swap
 *  target instead of just showing a raw error message. */
export async function deleteVoice(
  slug: string,
  replacement?: string,
): Promise<DeleteVoiceResult> {
  const qs = replacement ? `?replacement=${encodeURIComponent(replacement)}` : ''
  const r = await fetch(`${BASE}/voices/${slug}${qs}`, { method: 'DELETE' })
  if (r.status === 409) {
    const body = await r.json().catch(() => ({}))
    const detail = body?.detail ?? {}
    const err: VoiceInUseError = {
      status: 409,
      message: detail.message ?? 'voice is in use',
      referencing_projects: Array.isArray(detail.referencing_projects)
        ? detail.referencing_projects
        : [],
    }
    throw err
  }
  return ok<DeleteVoiceResult>(r)
}

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
  backend?: 'omnivoice' | 'higgs'
  license?: 'permissive' | 'non_commercial'
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
    backend?: 'omnivoice' | 'higgs'
    license?: 'permissive' | 'non_commercial'
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

export interface VoiceValidationText {
  text: string
  is_default: boolean
}

export const getVoiceValidationText = (): Promise<VoiceValidationText> =>
  fetch(`${BASE}/settings/voice-validation-text`).then(ok<VoiceValidationText>)

export const setVoiceValidationText = (text: string): Promise<VoiceValidationText> =>
  fetch(`${BASE}/settings/voice-validation-text`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  }).then(ok<VoiceValidationText>)

export const resetVoiceValidationText = (): Promise<VoiceValidationText> =>
  fetch(`${BASE}/settings/voice-validation-text`, { method: 'DELETE' }).then(
    ok<VoiceValidationText>,
  )

// ---- Backup / restore ----

export const backupPreview = (): Promise<BackupSize> =>
  fetch(`${BASE}/backup/preview`).then(ok<BackupSize>)

/** Returns the absolute /api URL to start a backup download. We don't
 *  fetch through ok() here — the response is a multi-MB ZIP stream
 *  that the browser downloads via an <a href> click instead. */
export function backupDownloadUrl(args: {
  includeRenders: boolean
  includeFinals: boolean
}): string {
  const qs = new URLSearchParams({
    include_renders: args.includeRenders ? 'true' : 'false',
    include_finals: args.includeFinals ? 'true' : 'false',
  })
  return `${BASE}/backup/export?${qs}`
}

export async function backupRestore(file: File): Promise<RestoreResult> {
  const fd = new FormData()
  fd.append('archive', file)
  return fetch(`${BASE}/backup/restore`, { method: 'POST', body: fd }).then(
    ok<RestoreResult>,
  )
}

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

export const updateProjectParams = (
  slug: string,
  params: Partial<Project['params']>,
  expectedVersion: number,
) => patchWithVersion<Project>(
  `${BASE}/projects/${slug}/params`, params, expectedVersion,
)

export const updateProjectBook = (
  slug: string,
  body: { language?: string },
  expectedVersion: number,
) => patchWithVersion<Project>(
  `${BASE}/projects/${slug}/book`, body, expectedVersion,
)

export const updateBlocksSkipped = (
  slug: string,
  indices: number[],
  expectedVersion: number,
) => patchWithVersion<Project>(
  `${BASE}/projects/${slug}/blocks-skipped`, { indices }, expectedVersion,
)

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

export interface PreviewVoicesEvents {
  onStarted?: (header: Omit<PreviewVoicesMatrix, 'voices'> & { total: number }) => void
  onCellDone?: (index: number, cell: VoicePreviewCell) => void
  onError?: (message: string) => void
}

/** Stream the voice-picker matrix. Backend renders the project's sample
 *  text once per requested voice slug; cells stream in via SSE so the
 *  UI shows progress instead of a blocked spinner. Resolves with the
 *  full matrix on the ``complete`` event. */
export async function previewVoices(
  slug: string,
  voiceSlugs: string[],
  events: PreviewVoicesEvents = {},
): Promise<PreviewVoicesMatrix> {
  const r = await fetch(`${BASE}/projects/${slug}/preview-voices`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ voice_slugs: voiceSlugs }),
  })
  if (!r.ok) {
    const detail = await r.text().catch(() => r.statusText)
    throw new Error(`${r.status} ${r.statusText}: ${detail}`)
  }
  if (!r.body) throw new Error('preview-voices: no response body')

  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let header: (Omit<PreviewVoicesMatrix, 'voices'> & { total: number }) | null = null
  const cells: VoicePreviewCell[] = []

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
        header = evt.data
        events.onStarted?.(evt.data)
      } else if (evt.event === 'cell_done') {
        cells[evt.data.index] = evt.data.voice
        events.onCellDone?.(evt.data.index, evt.data.voice)
      } else if (evt.event === 'error') {
        const msg = evt.data?.message ?? 'unknown error'
        events.onError?.(msg)
        throw new Error(msg)
      } else if (evt.event === 'complete') {
        if (!header) throw new Error('preview-voices: complete without started')
        return {
          sample_text: header.sample_text,
          sample_chars: header.sample_chars,
          sample_block_index: header.sample_block_index,
          sample_block_total: header.sample_block_total,
          total_book_chars: header.total_book_chars,
          num_step: header.num_step,
          guidance_scale: header.guidance_scale,
          speed: header.speed,
          voices: evt.data.voices,
        }
      }
    }
  }
  throw new Error('preview-voices: stream ended without complete event')
}

export const updateProjectVoice = (
  slug: string,
  voice_slug: string,
  expectedVersion: number,
) => patchWithVersion<Project>(
  `${BASE}/projects/${slug}/voice`, { voice_slug }, expectedVersion,
)

/** v0.5 — set the project's TTS engine. ``null`` / ``""`` / ``"default"``
 *  reset to stock OmniVoice; the backend normalises to ``null`` so the
 *  cache signature and DB row agree on one canonical form. Returns the
 *  updated project (with new `version`) so the caller can keep its
 *  optimistic-lock counter in sync without an extra GET. */
export const updateProjectTtsModel = (
  slug: string,
  tts_model: string | null,
  expectedVersion: number,
) => patchWithVersion<Project>(
  `${BASE}/projects/${slug}/tts-model`, { tts_model }, expectedVersion,
)

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
