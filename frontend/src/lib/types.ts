// TypeScript mirrors of the Pydantic response models in audiomat/api.py.
// Kept in sync manually for v0.1; later we can generate from OpenAPI.

export interface Voice {
  name: string
  name_slug: string
  duration_s: number
  sample_rate: number
  channels: number
  transcript_chars: number
  notes: string
  created: string
}

export interface BookInfo {
  filename: string
  blocks_total: number
  blocks_skipped: number[]
  title: string | null
  author: string | null
  language: string | null
}

export interface RenderParams {
  num_step: number
  guidance_scale: number
  speed: number
  min_chars: number
  max_chars: number
  target_lufs: number
  silence_gap_ms: number
  section_headers: string[]
}

export interface ProjectStatus {
  chapters_done: number
  chapters_total: number
  last_completed: string | null
  phase: 'draft' | 'preview' | 'rendering' | 'complete' | 'failed'
}

export interface Project {
  name: string
  name_slug: string
  book: BookInfo
  voice_ref: string
  voice_ref_slug: string
  params: RenderParams
  status: ProjectStatus
  created: string
  last_run: string
  has_final_m4b: boolean
}

export type ProgressEventKind =
  | 'render_start'
  | 'chunk_synthed'
  | 'chunk_cached'
  | 'chapter_concat_start'
  | 'chapter_done'
  | 'chapter_skipped'
  | 'render_complete'
  | 'error'

export interface ProgressEvent {
  kind: ProgressEventKind
  chapter_idx: number
  chapter_total: number
  chapter_stem: string
  chunk_idx: number
  chunk_total: number
  text: string
  gen_seconds: number
  duration_s: number
  rtf: number
  message: string
}

export interface DraftUploadResult {
  audio_path: string
  duration_s: number
  sample_rate: number
  channels: number
  warning: string
}
