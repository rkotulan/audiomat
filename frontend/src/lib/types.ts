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
  // Optional slug of a registered TTS model the voice prefers. Null /
  // empty means use the stock OmniVoice. Editable via PATCH
  // /voices/{slug}/model.
  tts_model: string | null
}

export interface TTSModel {
  name: string
  name_slug: string
  source_type: 'local' | 'hf'
  source_ref: string                // local: src path; hf: "<org>/<repo>"
  hf_revision: string | null
  size_bytes: number
  notes: string
  created: string
}

export interface HFRepoInfo {
  repo_id: string
  private: boolean
  last_modified: string
  size_bytes: number
  tags: string[]
}

export interface HFTokenStatus {
  has_token: boolean
  source: 'env' | 'secrets_file' | null
}

export interface BackupSize {
  essentials_bytes: number
  renders_bytes: number
  finals_bytes: number
  file_counts: { essentials?: number; renders?: number; finals?: number }
}

export interface RestoreResult {
  files_extracted: number
  bytes_extracted: number
  // Absolute path to the auto-snapshot of pre-restore essentials.
  // Surfaced in the success message so the user knows where rollback
  // material lives (or null if the snapshot couldn't be written —
  // restore still proceeded).
  pre_restore_snapshot: string | null
  warnings: string[]
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
  // Optimistic-lock counter — echo back as `If-Match` on PATCH so the
  // server can detect "another tab edited this since you loaded it"
  // and respond 409 instead of silently overwriting.
  version: number
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
  text_chars: number
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

// ---- Long-source voice picker (multi-step wizard) ----

export interface ChapterMarker {
  index: number
  title: string
  start_s: number
  end_s: number
  duration_s: number
}

export interface DraftUploadLongResult {
  audio_path: string                  // converted full WAV inside tempdir
  duration_s: number
  sample_rate: number
  channels: number
  chapters: ChapterMarker[]           // empty when source has none
}

export interface VoiceCandidate {
  index: number
  start_s: number                     // relative to the analyzed slice
  end_s: number
  duration_s: number
  score: number                       // 0-100 composite
  preview_path: string                // pre-trimmed WAV in tempdir, served via draftAudioUrl
  breakdown: {
    density: number
    density_score: number
    rms_cv: number
    consistency_score: number
    peak: number
    clipping_score: number
    snr_db: number | null             // null when too little silence to measure
    snr_score: number
  }
}

export interface AnalyzeResult {
  candidates: VoiceCandidate[]
  analyzed_start_s: number
  analyzed_end_s: number
  full_audio_path: string
}

export interface StagedVoicePreview {
  audio_path: string                  // tempdir-served via draftAudioUrl
  duration_s: number
  gen_seconds: number                 // 0 on cache hit
}

export interface PreviewVariant {
  label: 'Fast' | 'Balanced' | 'Crisp' | 'Stable'
  num_step: number
  guidance_scale: number
  speed: number
  audio_url: string
  cached: boolean
  gen_seconds: number
  duration_s: number
  // Server-side flag: true when the user has previously fine-tuned this
  // cell and the override is persisted in previews/_tuned_cells.json.
  // Survives page refresh.
  tuned?: boolean
}

export interface PreviewMatrix {
  sample_text: string
  sample_chars: number
  sample_block_index: number
  sample_block_total: number
  total_book_chars: number
  variants: PreviewVariant[]
}

export interface VoicePreviewCell {
  voice_slug: string
  voice_name: string
  audio_url: string
  cached: boolean
  gen_seconds: number
  duration_s: number
}

export interface PreviewVoicesMatrix {
  sample_text: string
  sample_chars: number
  sample_block_index: number
  sample_block_total: number
  total_book_chars: number
  // Project params used to render the cells. Echoed so the UI can show
  // "rendered at 48/2.0/1.0" — useful when the user later changes
  // params and wonders why the cached samples sound a bit different
  // from a fresh quality preview.
  num_step: number
  guidance_scale: number
  speed: number
  voices: VoicePreviewCell[]
}

export type ChapterStatus = 'skipped' | 'pending' | 'rendered' | 'rendering' | 'failed'

export interface Chapter {
  block_index: number
  renderable_index: number | null   // null when skipped
  stem: string | null               // null when skipped
  char_count: number
  preview: string
  status: ChapterStatus
  audio_url: string | null
  duration_s: number | null
  has_override: boolean             // true when this chapter has a per-block text override
}

export interface ChapterAutoPause {
  header: string
  type: 'time_marker' | 'section_header' | 'unknown'
}

export interface ChapterText {
  stem: string
  block_index: number
  renderable_index: number
  text: string                      // current text (override if present, else EPUB original)
  original_text: string             // EPUB original text — never changes
  has_override: boolean
  char_count: number
  estimated_chunks: number
  min_chars: number
  max_chars: number
  auto_pause: ChapterAutoPause | null
}

export interface ChaptersResponse {
  chapters: Chapter[]
  renderable_total: number
  rendered_count: number
}

export interface CustomPreviewResult {
  num_step: number
  guidance_scale: number
  speed: number
  sample_text: string
  sample_chars: number
  sample_block_index: number
  sample_block_total: number
  total_book_chars: number
  audio_url: string
  cached: boolean
  gen_seconds: number
  duration_s: number
}

/** Streamed by POST /api/models/from-hf and /api/models/{slug}/redownload. */
export interface ModelDownloadEvent {
  kind: 'started' | 'progress' | 'complete' | 'error'
  downloaded_bytes: number
  total_bytes: number
  percent: number
  message: string | null
  model_slug: string | null
}
