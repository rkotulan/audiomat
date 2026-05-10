import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import {
  ArrowLeft,
  ArrowRight,
  Play,
  Hammer,
  Download,
  Trash2,
  Wand2,
  Save,
  Star,
  Sliders,
  CheckCheck,
  RotateCw,
  Ban,
  Undo2,
  Square,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Progress } from '@/components/ui/progress'
import { Badge } from '@/components/ui/badge'
import { Slider } from '@/components/ui/slider'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  buildM4b,
  cancelRender,
  deleteProject,
  getProject,
  listChapters,
  previewCustom,
  previewMatrix,
  projectM4bUrl,
  startRender,
  subscribeProgress,
  updateBlocksSkipped,
  updateProjectParams,
} from '@/lib/api'
import type {
  Chapter,
  ChaptersResponse,
  CustomPreviewResult,
  PreviewMatrix,
  Project,
  ProgressEvent,
} from '@/lib/types'

export function ProjectDetail() {
  const { slug = '' } = useParams()
  const nav = useNavigate()

  const [project, setProject] = useState<Project | null>(null)
  const [tab, setTab] = useState<string>('overview')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [renderEvents, setRenderEvents] = useState<ProgressEvent[]>([])
  const [latest, setLatest] = useState<ProgressEvent | null>(null)
  const [chapters, setChapters] = useState<ChaptersResponse | null>(null)
  const [selectedIndices, setSelectedIndices] = useState<Set<number>>(new Set())
  const unsubRef = useRef<(() => void) | null>(null)

  const refreshChapters = async () => {
    try {
      const c = await listChapters(slug)
      setChapters(c)
    } catch (e) {
      // soft-fail; the chapter list is best-effort
      console.warn('listChapters failed:', e)
    }
  }

  const refresh = () =>
    getProject(slug)
      .then(setProject)
      .catch((e) => setErr(String(e)))

  useEffect(() => {
    refresh()
    refreshChapters()
    return () => unsubRef.current?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug])

  const startRenderJob = async (indices?: number[]) => {
    setErr('')
    setBusy(true)
    setRenderEvents([])
    setLatest(null)
    try {
      await startRender(slug, indices)
      unsubRef.current = subscribeProgress(slug, (e) => {
        setLatest(e)
        setRenderEvents((prev) => [...prev.slice(-200), e])
        // Live-patch the chapter list as events flow in. This keeps the
        // Chapters table in sync without polling — auto-refresh via SSE.
        if (e.kind === 'chapter_concat_start' && e.chapter_stem) {
          setChapters((prev) => prev && patchChapterStatus(prev, e.chapter_stem, 'rendering'))
        } else if (e.kind === 'chapter_done' && e.chapter_stem) {
          setChapters((prev) =>
            prev &&
            patchChapterStatus(prev, e.chapter_stem, 'rendered', `${BASE_API}/projects/${slug}/chapter-audio/${encodeURIComponent(e.chapter_stem)}`),
          )
        } else if (e.kind === 'chapter_skipped' && e.chapter_stem) {
          // chunk cache hit + final wav present → already rendered
          setChapters((prev) =>
            prev &&
            patchChapterStatus(prev, e.chapter_stem, 'rendered', `${BASE_API}/projects/${slug}/chapter-audio/${encodeURIComponent(e.chapter_stem)}`),
          )
        } else if (e.kind === 'error') {
          if (e.chapter_stem) {
            setChapters((prev) => prev && patchChapterStatus(prev, e.chapter_stem, 'failed'))
          }
        }
        if (e.kind === 'render_complete' || e.kind === 'error') {
          unsubRef.current?.()
          unsubRef.current = null
          setBusy(false)
          refresh()
          // Final canonical refresh — picks up duration_s for newly rendered.
          refreshChapters()
        }
      })
    } catch (e) {
      setErr(String(e))
      setBusy(false)
    }
  }

  const onRender = () => startRenderJob()
  const onRenderSelected = () => {
    if (selectedIndices.size === 0) return
    startRenderJob(Array.from(selectedIndices).sort((a, b) => a - b))
  }
  const onRenderPending = () => {
    if (!chapters) return
    const pending = chapters.chapters
      .filter((c) => c.status === 'pending' && c.renderable_index != null)
      .map((c) => c.renderable_index as number)
    if (pending.length === 0) return
    startRenderJob(pending)
  }
  const [cancelling, setCancelling] = useState(false)
  const onStopRender = async () => {
    if (!busy || cancelling) return
    setCancelling(true)
    try {
      await cancelRender(slug)
    } catch (e) {
      alert(`Cancel failed: ${e}`)
    } finally {
      // The SSE error event will arrive next, flipping busy=false.
      // We just lock the button until it does.
      setTimeout(() => setCancelling(false), 4000)
    }
  }

  const onBuildM4b = async () => {
    setBusy(true)
    setErr('')
    try {
      await buildM4b(slug)
      refresh()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const onDelete = async () => {
    if (!confirm(`Delete project "${project?.name}"? This is permanent.`)) return
    try {
      await deleteProject(slug)
      nav('/projects')
    } catch (e) {
      setErr(String(e))
    }
  }

  if (!project) {
    return (
      <div className="space-y-4">
        <Button variant="ghost" onClick={() => nav('/projects')} className="-ml-3">
          <ArrowLeft className="h-4 w-4" />
          Back
        </Button>
        {err ? (
          <div className="text-sm text-destructive">{err}</div>
        ) : (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}
      </div>
    )
  }

  const pct = project.status.chapters_total
    ? Math.round((project.status.chapters_done / project.status.chapters_total) * 100)
    : 0

  return (
    <div className="space-y-6">
      <div>
        <Button variant="ghost" onClick={() => nav('/projects')} className="-ml-3">
          <ArrowLeft className="h-4 w-4" />
          Back to projects
        </Button>
        <div className="mt-2 flex items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">{project.name}</h1>
            <p className="text-sm text-muted-foreground">
              {project.book.author && (
                <>
                  by {project.book.author} · {project.book.language ?? 'cs'} ·{' '}
                </>
              )}
              voice <span className="font-mono">{project.voice_ref}</span>
            </p>
          </div>
          <Badge variant={phaseVariant(project.status.phase)}>
            {project.status.phase}
          </Badge>
        </div>
      </div>

      {err && (
        <div className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
          {err}
        </div>
      )}

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="preview">Preview</TabsTrigger>
          <TabsTrigger value="render">Render</TabsTrigger>
          <TabsTrigger value="output">Output</TabsTrigger>
          <TabsTrigger value="advanced">Advanced</TabsTrigger>
        </TabsList>

        <TabsContent value="overview" className="space-y-4 pt-4">
          <Card>
            <CardHeader>
              <CardTitle>Book</CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <Row label="Title" value={project.book.title ?? '—'} />
              <Row label="Author" value={project.book.author ?? '—'} />
              <Row label="Language" value={project.book.language ?? '—'} />
              <Row label="Filename" value={project.book.filename} mono />
              <Row label="Blocks total" value={String(project.book.blocks_total)} mono />
              <Row
                label="Skipped"
                value={
                  project.book.blocks_skipped.length
                    ? project.book.blocks_skipped.join(', ')
                    : '—'
                }
                mono
              />
            </CardContent>
          </Card>

          <div className="flex justify-end">
            <Button onClick={() => setTab('preview')}>
              Next: preview voice
              <ArrowRight className="h-4 w-4" />
            </Button>
          </div>
        </TabsContent>

        <TabsContent value="preview" className="space-y-4 pt-4">
          <PreviewTab
            project={project}
            slug={slug}
            onApply={refresh}
            onPicked={() => setTab('render')}
          />
        </TabsContent>

        <TabsContent value="render" className="space-y-4 pt-4">
          <Card>
            <CardHeader>
              <CardTitle>Progress</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div>
                <div className="flex justify-between text-sm mb-1">
                  <span>
                    {chapters
                      ? `${chapters.rendered_count}/${chapters.renderable_total} chapters rendered`
                      : `Chapter ${latest?.chapter_idx ?? project.status.chapters_done}/${latest?.chapter_total ?? project.status.chapters_total}`}
                  </span>
                  <span className="text-muted-foreground">
                    {chapters && chapters.renderable_total
                      ? Math.round((chapters.rendered_count / chapters.renderable_total) * 100)
                      : pct}%
                  </span>
                </div>
                <Progress
                  value={
                    chapters && chapters.renderable_total
                      ? Math.round((chapters.rendered_count / chapters.renderable_total) * 100)
                      : pct
                  }
                />
              </div>

              <div className="flex flex-wrap gap-2">
                <Button onClick={onRender} disabled={busy}>
                  <Play className="h-4 w-4" />
                  {busy ? 'Rendering…' : 'Render all'}
                </Button>
                <Button
                  variant="outline"
                  onClick={onRenderPending}
                  disabled={
                    busy ||
                    !chapters ||
                    chapters.chapters.every((c) => c.status !== 'pending')
                  }
                >
                  <RotateCw className="h-4 w-4" />
                  Render pending
                </Button>
                <Button
                  variant="outline"
                  onClick={onRenderSelected}
                  disabled={busy || selectedIndices.size === 0}
                >
                  <CheckCheck className="h-4 w-4" />
                  Render selected ({selectedIndices.size})
                </Button>
                {busy && (
                  <Button
                    variant="destructive"
                    onClick={onStopRender}
                    disabled={cancelling}
                    className="ml-auto"
                  >
                    <Square className="h-4 w-4 fill-current" />
                    {cancelling ? 'Stopping…' : 'Stop'}
                  </Button>
                )}
              </div>

              {renderEvents.length > 0 && (
                <details className="rounded-md border bg-card">
                  <summary className="cursor-pointer select-none list-none px-3 py-2 text-sm font-medium hover:bg-accent/50">
                    Event log ({renderEvents.length})
                  </summary>
                  <div className="border-t max-h-[260px] overflow-y-auto font-mono text-xs">
                    {renderEvents.slice().reverse().map((e, i) => (
                      <EventRow key={i} event={e} />
                    ))}
                  </div>
                </details>
              )}
            </CardContent>
          </Card>

          <ChaptersListCard
            slug={slug}
            chapters={chapters}
            selectedIndices={selectedIndices}
            setSelectedIndices={setSelectedIndices}
            onRefresh={refreshChapters}
            onToggleSkip={async (blockIndex, becomeSkipped) => {
              if (!chapters) return
              const current = chapters.chapters
                .filter((c) => c.status === 'skipped')
                .map((c) => c.block_index)
              const next = becomeSkipped
                ? [...current, blockIndex]
                : current.filter((i) => i !== blockIndex)
              try {
                await updateBlocksSkipped(slug, next)
                await refreshChapters()
                refresh()
              } catch (e) {
                alert(`Failed to update skip list: ${e}`)
              }
            }}
          />
        </TabsContent>

        <TabsContent value="output" className="space-y-4 pt-4">
          <Card>
            <CardHeader>
              <CardTitle>M4B</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {project.has_final_m4b ? (
                <>
                  <p className="text-sm text-muted-foreground">
                    M4B has been built. Download or rebuild if you re-rendered chapters.
                  </p>
                  <div className="flex gap-2">
                    <Button asChild>
                      <a href={projectM4bUrl(slug)} download>
                        <Download className="h-4 w-4" />
                        Download M4B
                      </a>
                    </Button>
                    <Button variant="outline" onClick={onBuildM4b} disabled={busy}>
                      <Hammer className="h-4 w-4" />
                      Rebuild M4B
                    </Button>
                  </div>
                </>
              ) : (
                <>
                  <p className="text-sm text-muted-foreground">
                    M4B not built yet. Run render first, then build M4B to concatenate
                    chapter WAVs into the final audiobook with chapter markers.
                  </p>
                  <Button
                    onClick={onBuildM4b}
                    disabled={busy || project.status.chapters_done === 0}
                  >
                    <Hammer className="h-4 w-4" />
                    Build M4B
                  </Button>
                </>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="advanced" className="space-y-4 pt-4">
          <OutputParamsCard project={project} slug={slug} onSaved={refresh} />
          <Card>
            <CardHeader>
              <CardTitle className="text-destructive">Danger zone</CardTitle>
            </CardHeader>
            <CardContent>
              <Button variant="destructive" onClick={onDelete}>
                <Trash2 className="h-4 w-4" />
                Delete project
              </Button>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>

      <p className="text-xs text-muted-foreground">
        <Link to="/projects" className="underline">
          ← All projects
        </Link>
      </p>
    </div>
  )
}

function Row({
  label,
  value,
  mono,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <>
      <span className="text-muted-foreground">{label}</span>
      <span className={mono ? 'font-mono text-right' : 'text-right'}>{value}</span>
    </>
  )
}

function EventRow({ event }: { event: ProgressEvent }) {
  let line = ''
  switch (event.kind) {
    case 'render_start':
      line = `▶ render started — ${event.message}`
      break
    case 'chunk_synthed':
      line = `[${event.chapter_idx}/${event.chapter_total}] chunk ${event.chunk_idx + 1}/${event.chunk_total} ${event.gen_seconds.toFixed(2)}s rtf=${event.rtf.toFixed(2)} dur=${event.duration_s.toFixed(1)}s`
      break
    case 'chunk_cached':
      line = `[${event.chapter_idx}/${event.chapter_total}] chunk ${event.chunk_idx + 1}/${event.chunk_total} cached ✓`
      break
    case 'chapter_concat_start':
      line = `[${event.chapter_idx}] concat+loudnorm…`
      break
    case 'chapter_done':
      line = `✓ [${event.chapter_idx}] ${event.chapter_stem}`
      break
    case 'chapter_skipped':
      line = `↳ [${event.chapter_idx}] ${event.chapter_stem} ${event.message}`
      break
    case 'render_complete':
      line = `✓ render complete — ${event.message}`
      break
    case 'error':
      line = `✗ ${event.message}`
      break
  }
  const cls =
    event.kind === 'error'
      ? 'text-destructive'
      : event.kind === 'render_complete' || event.kind === 'chapter_done'
        ? 'text-emerald-600 dark:text-emerald-400'
        : event.kind === 'chunk_cached' || event.kind === 'chapter_skipped'
          ? 'text-muted-foreground'
          : ''
  return <div className={`px-3 py-1 ${cls}`}>{line}</div>
}

function phaseVariant(
  phase: Project['status']['phase'],
): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (phase === 'complete') return 'default'
  if (phase === 'failed') return 'destructive'
  if (phase === 'rendering') return 'secondary'
  return 'outline'
}

const BASE_API = '/api'

function patchChapterStatus(
  resp: ChaptersResponse,
  stem: string,
  status: Chapter['status'],
  audioUrl?: string,
): ChaptersResponse {
  let newRendered = resp.rendered_count
  const next = resp.chapters.map((c) => {
    if (c.stem !== stem) return c
    const becomingRendered = status === 'rendered' && c.status !== 'rendered'
    if (becomingRendered) newRendered += 1
    return {
      ...c,
      status,
      audio_url: audioUrl !== undefined ? audioUrl : c.audio_url,
    }
  })
  return { ...resp, chapters: next, rendered_count: newRendered }
}

/**
 * Estimate full-book render wall-time for a given variant.
 *
 * Linear scaling by chars: if the sample's `sample_chars` rendered in
 * `gen_seconds`, the whole book of `total_book_chars` should take
 * `(total / sample) * gen_seconds`. Includes a small per-chapter
 * overhead bump (~10 %) for ffmpeg loudnorm + concat passes.
 *
 * Cached variants have gen_seconds=0; we return "—" for those (no
 * fresh measurement). User can re-tune to force a re-render.
 */
function estimateBookRender(
  matrix: PreviewMatrix,
  variant: PreviewMatrix['variants'][number],
): string {
  if (variant.cached || variant.gen_seconds <= 0) {
    return '— (re-tune to estimate)'
  }
  const ratio = matrix.total_book_chars / Math.max(matrix.sample_chars, 1)
  const seconds = ratio * variant.gen_seconds * 1.1   // +10% concat overhead
  return formatDuration(seconds)
}

function formatDuration(seconds: number): string {
  if (!isFinite(seconds) || seconds <= 0) return '—'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (h === 0) return `~${m} min`
  return `~${h} h ${m.toString().padStart(2, '0')} min`
}

/** mm:ss / h:mm:ss for per-chapter audio durations (no "~" prefix). */
function formatChapterTime(seconds: number): string {
  if (!isFinite(seconds) || seconds <= 0) return '—'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = Math.floor(seconds % 60)
  const mm = m.toString().padStart(2, '0')
  const ss = s.toString().padStart(2, '0')
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`
}

// ----------------------------------------------------------------------------
// Preview tab — 4-cell parameter matrix
// ----------------------------------------------------------------------------

function PreviewTab({
  project,
  slug,
  onApply,
  onPicked,
}: {
  project: Project
  slug: string
  onApply: () => void
  onPicked: () => void
}) {
  const [matrix, setMatrix] = useState<PreviewMatrix | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [tuningIndex, setTuningIndex] = useState<number | null>(null)
  const [tunedFlags, setTunedFlags] = useState<Set<number>>(new Set())

  const onGenerate = async () => {
    setBusy(true)
    setErr('')
    try {
      const m = await previewMatrix(slug)
      setMatrix(m)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const onPick = async (variant: PreviewMatrix['variants'][number]) => {
    try {
      await updateProjectParams(slug, {
        num_step: variant.num_step,
        guidance_scale: variant.guidance_scale,
        speed: variant.speed,
      })
      onApply()
      onPicked()
    } catch (e) {
      alert(`Failed to apply params: ${e}`)
    }
  }

  const onTuned = (idx: number, custom: CustomPreviewResult) => {
    if (!matrix) return
    const variants = [...matrix.variants]
    variants[idx] = {
      ...variants[idx],
      num_step: custom.num_step,
      guidance_scale: custom.guidance_scale,
      speed: custom.speed,
      audio_url: custom.audio_url,
      cached: custom.cached,
      gen_seconds: custom.gen_seconds,
      duration_s: custom.duration_s,
    }
    setMatrix({ ...matrix, variants })
    setTunedFlags(new Set(tunedFlags).add(idx))
    setTuningIndex(null)
  }

  const isCurrent = (v: PreviewMatrix['variants'][number]) =>
    project.params.num_step === v.num_step &&
    Math.abs(project.params.guidance_scale - v.guidance_scale) < 0.01 &&
    Math.abs(project.params.speed - v.speed) < 0.01

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Parameter matrix</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Synthesize a representative ~30 s sample from the book at four
            common parameter combinations. Listen, pick the variant that
            sounds best, and the project's render params will switch to it.
          </p>
          <p className="text-xs text-muted-foreground">
            First call: ~22 s (sequential OmniVoice inference for 4 cells).
            Subsequent calls re-use the cache.
          </p>
          <div className="flex gap-2">
            <Button onClick={onGenerate} disabled={busy}>
              <Wand2 className="h-4 w-4" />
              {busy ? 'Generating…' : matrix ? 'Re-generate' : 'Generate matrix'}
            </Button>
          </div>
          {err && <div className="text-sm text-destructive">{err}</div>}
          {matrix && (
            <div className="rounded-md border bg-muted/40 p-3 text-xs space-y-1">
              <p className="font-medium">
                Sample text — block {matrix.sample_block_index + 1} of{' '}
                {matrix.sample_block_total} ({matrix.sample_chars} chars)
              </p>
              <p className="italic line-clamp-3">{matrix.sample_text}</p>
              <p className="text-muted-foreground">
                Picked from ~⅓ in to skip front-matter / DRM watermarks.
                Edit <code>book.blocks_skipped</code> in config.json to
                exclude specific block indices.
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {matrix && (
        <div className="grid gap-4 md:grid-cols-2">
          {matrix.variants.map((v, idx) => (
            <Card key={v.label} className={isCurrent(v) ? 'border-primary' : ''}>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center justify-between">
                  <span className="flex items-center gap-2">
                    {v.label}
                    {tunedFlags.has(idx) && (
                      <Badge variant="secondary" className="font-normal">
                        modified
                      </Badge>
                    )}
                    {isCurrent(v) && (
                      <Badge variant="default" className="font-normal">
                        <Star className="h-3 w-3" /> current
                      </Badge>
                    )}
                  </span>
                  <span className="text-xs text-muted-foreground font-normal font-mono">
                    {v.duration_s.toFixed(1)} s
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <div className="grid grid-cols-3 gap-2 text-xs text-muted-foreground">
                  <span>num_step <span className="font-mono text-foreground">{v.num_step}</span></span>
                  <span>gs <span className="font-mono text-foreground">{v.guidance_scale.toFixed(1)}</span></span>
                  <span>speed <span className="font-mono text-foreground">{v.speed.toFixed(2)}</span></span>
                </div>
                <audio
                  controls
                  src={v.audio_url}
                  preload="metadata"
                  className="w-full h-9"
                />
                <div className="flex items-center justify-between rounded-md bg-muted/40 px-2 py-1.5 text-xs">
                  <span className="text-muted-foreground">Est. full book render:</span>
                  <span className="font-mono">
                    {estimateBookRender(matrix, v)}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-muted-foreground">
                    {v.cached ? 'cached' : `${v.gen_seconds.toFixed(1)} s sample`}
                  </span>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setTuningIndex(idx)}
                    >
                      <Sliders className="h-3 w-3" />
                      Fine tune
                    </Button>
                    <Button size="sm" onClick={() => onPick(v)}>
                      Use this
                      <ArrowRight className="h-3 w-3" />
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <FineTuneDialog
        open={tuningIndex !== null}
        variant={tuningIndex !== null && matrix ? matrix.variants[tuningIndex] : null}
        slug={slug}
        onClose={() => setTuningIndex(null)}
        onTuned={(custom) => {
          if (tuningIndex !== null) onTuned(tuningIndex, custom)
        }}
      />
    </div>
  )
}

// ----------------------------------------------------------------------------
// Fine-tune dialog — modal with 3 voice-synth sliders bound to one variant.
// Generate calls /preview-custom; on success the parent matrix swaps in
// the new audio + params for that variant.
// ----------------------------------------------------------------------------

function FineTuneDialog({
  open,
  variant,
  slug,
  onClose,
  onTuned,
}: {
  open: boolean
  variant:
    | (PreviewMatrix['variants'][number])
    | null
  slug: string
  onClose: () => void
  onTuned: (custom: CustomPreviewResult) => void
}) {
  const [params, setParams] = useState({
    num_step: variant?.num_step ?? 48,
    guidance_scale: variant?.guidance_scale ?? 2.0,
    speed: variant?.speed ?? 1.0,
  })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  // Re-init local sliders whenever the dialog opens for a different variant.
  useEffect(() => {
    if (variant) {
      setParams({
        num_step: variant.num_step,
        guidance_scale: variant.guidance_scale,
        speed: variant.speed,
      })
      setErr('')
    }
  }, [variant])

  const onGenerate = async () => {
    setBusy(true)
    setErr('')
    try {
      const result = await previewCustom(slug, params)
      onTuned(result)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Fine tune {variant?.label ?? ''}</DialogTitle>
          <DialogDescription>
            Adjust voice-synthesis parameters. Generate re-renders this card
            with the new sample.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5 py-2">
          <SliderRow
            label="num_step"
            hint="Diffusion steps. Higher = smoother, ~1.5× slower per +16."
            value={params.num_step}
            min={16}
            max={64}
            step={16}
            format={(v) => String(v)}
            onChange={(v) => setParams({ ...params, num_step: v })}
          />
          <SliderRow
            label="guidance_scale"
            hint="Conditioning strength. 2.0 default; 3.0+ may over-emphasize."
            value={params.guidance_scale}
            min={1.0}
            max={4.0}
            step={0.1}
            format={(v) => v.toFixed(1)}
            onChange={(v) => setParams({ ...params, guidance_scale: v })}
          />
          <SliderRow
            label="speed"
            hint="Speech tempo. 1.0 natural, 0.85 relaxed, 1.15 brisk."
            value={params.speed}
            min={0.7}
            max={1.3}
            step={0.05}
            format={(v) => v.toFixed(2) + '×'}
            onChange={(v) => setParams({ ...params, speed: v })}
          />
        </div>

        {err && <p className="text-xs text-destructive">{err}</p>}

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={onGenerate} disabled={busy}>
            <Wand2 className="h-4 w-4" />
            {busy ? 'Generating…' : 'Generate'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ----------------------------------------------------------------------------
// OutputParamsCard — chunking + loudness knobs for the actual book render
// (NOT for preview audio). Lives in the Advanced tab.
// ----------------------------------------------------------------------------

function OutputParamsCard({
  project,
  slug,
  onSaved,
}: {
  project: Project
  slug: string
  onSaved: () => void
}) {
  const [params, setParams] = useState({
    min_chars: project.params.min_chars,
    max_chars: project.params.max_chars,
    target_lufs: project.params.target_lufs,
    silence_gap_ms: project.params.silence_gap_ms,
  })
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  const dirty =
    params.min_chars !== project.params.min_chars ||
    params.max_chars !== project.params.max_chars ||
    params.target_lufs !== project.params.target_lufs ||
    params.silence_gap_ms !== project.params.silence_gap_ms

  const onSave = async () => {
    setSaving(true)
    setMsg('')
    try {
      await updateProjectParams(slug, params)
      setMsg('Saved.')
      onSaved()
    } catch (e) {
      setMsg(`Failed: ${e}`)
    } finally {
      setSaving(false)
    }
  }

  const onReset = () =>
    setParams({
      min_chars: project.params.min_chars,
      max_chars: project.params.max_chars,
      target_lufs: project.params.target_lufs,
      silence_gap_ms: project.params.silence_gap_ms,
    })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Output parameters</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          These knobs affect the chapter-level concat + loudness pass on the
          rendered book — they don't change preview audio (preview is a single
          raw chunk). Voice-synth params (num_step / gs / speed) live on the{' '}
          Preview tab via the per-variant Fine&nbsp;tune dialog.
        </p>

        <div className="grid grid-cols-2 gap-4">
          <NumberRow
            label="min_chars"
            hint="Chunk floor (info only)"
            value={params.min_chars}
            onChange={(v) => setParams({ ...params, min_chars: v })}
          />
          <NumberRow
            label="max_chars"
            hint="Chunk cap (90–250 typical)"
            value={params.max_chars}
            onChange={(v) => setParams({ ...params, max_chars: v })}
          />
          <NumberRow
            label="target_lufs"
            hint="-23 classic, -16 audiobook, -14 louder"
            value={params.target_lufs}
            onChange={(v) => setParams({ ...params, target_lufs: v })}
            step={0.5}
          />
          <NumberRow
            label="silence_gap_ms"
            hint="Inter-chunk pause"
            value={params.silence_gap_ms}
            onChange={(v) => setParams({ ...params, silence_gap_ms: v })}
          />
        </div>

        <div className="flex items-center justify-between pt-2">
          <span className="text-xs text-muted-foreground">{msg}</span>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onReset} disabled={!dirty}>
              Reset
            </Button>
            <Button onClick={onSave} disabled={!dirty || saving}>
              <Save className="h-4 w-4" />
              {saving ? 'Saving…' : 'Save'}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function SliderRow({
  label,
  hint,
  value,
  min,
  max,
  step,
  format,
  onChange,
}: {
  label: string
  hint: string
  value: number
  min: number
  max: number
  step: number
  format: (v: number) => string
  onChange: (v: number) => void
}) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <Label className="font-mono">{label}</Label>
        <span className="font-mono text-sm">{format(value)}</span>
      </div>
      <Slider
        value={[value]}
        min={min}
        max={max}
        step={step}
        onValueChange={(v) => onChange(v[0] ?? value)}
      />
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  )
}

function NumberRow({
  label,
  hint,
  value,
  onChange,
  step = 1,
}: {
  label: string
  hint: string
  value: number
  onChange: (v: number) => void
  step?: number
}) {
  return (
    <div className="space-y-1">
      <Label className="font-mono text-xs">{label}</Label>
      <Input
        type="number"
        step={step}
        value={value}
        onChange={(e) => {
          const n = Number(e.target.value)
          if (!Number.isNaN(n)) onChange(n)
        }}
      />
      <p className="text-xs text-muted-foreground">{hint}</p>
    </div>
  )
}

// ----------------------------------------------------------------------------
// Chapters list — checkboxes + status badges + inline per-chapter audio.
// Auto-refreshed via SSE events from the parent (patchChapterStatus). Manual
// refresh button as a safety net.
// ----------------------------------------------------------------------------

function ChaptersListCard({
  chapters,
  selectedIndices,
  setSelectedIndices,
  onRefresh,
  onToggleSkip,
}: {
  slug: string
  chapters: ChaptersResponse | null
  selectedIndices: Set<number>
  setSelectedIndices: (s: Set<number>) => void
  onRefresh: () => void
  onToggleSkip: (blockIndex: number, becomeSkipped: boolean) => void | Promise<void>
}) {
  if (!chapters) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Chapters</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">Loading chapters…</p>
        </CardContent>
      </Card>
    )
  }

  const renderable = chapters.chapters.filter((c) => c.renderable_index != null)
  const renderableIndices = renderable
    .map((c) => c.renderable_index as number)

  const allSelected =
    renderable.length > 0 && renderable.every((c) => selectedIndices.has(c.renderable_index!))
  const someSelected = !allSelected && renderableIndices.some((i) => selectedIndices.has(i))

  const toggleAll = () => {
    if (allSelected) {
      setSelectedIndices(new Set())
    } else {
      setSelectedIndices(new Set(renderableIndices))
    }
  }
  const toggleOne = (idx: number) => {
    const next = new Set(selectedIndices)
    if (next.has(idx)) next.delete(idx)
    else next.add(idx)
    setSelectedIndices(next)
  }
  const selectPending = () => {
    const pending = renderable
      .filter((c) => c.status === 'pending')
      .map((c) => c.renderable_index as number)
    setSelectedIndices(new Set(pending))
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between">
          <span>Chapters</span>
          <span className="text-xs text-muted-foreground font-normal">
            {chapters.rendered_count} rendered ·{' '}
            {renderable.length - chapters.rendered_count} pending ·{' '}
            {chapters.chapters.length - renderable.length} skipped
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-center gap-3 pb-2 border-b">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <Checkbox
              checked={allSelected ? true : someSelected ? 'indeterminate' : false}
              onCheckedChange={toggleAll}
            />
            <span>{allSelected ? 'Deselect all' : 'Select all'}</span>
          </label>
          <Button size="sm" variant="ghost" onClick={selectPending}>
            Select pending
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setSelectedIndices(new Set())}>
            Clear
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto"
            onClick={onRefresh}
            title="Refresh chapter list"
          >
            <RotateCw className="h-3 w-3" />
            Refresh
          </Button>
        </div>

        <div className="max-h-[600px] overflow-y-auto rounded-md border">
          <table className="w-full text-sm">
            <tbody>
              {chapters.chapters.map((c) => (
                <ChapterRow
                  key={c.block_index}
                  chapter={c}
                  selected={
                    c.renderable_index != null && selectedIndices.has(c.renderable_index)
                  }
                  onToggle={() =>
                    c.renderable_index != null && toggleOne(c.renderable_index)
                  }
                  onToggleSkip={() =>
                    onToggleSkip(c.block_index, c.status !== 'skipped')
                  }
                />
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  )
}

function ChapterRow({
  chapter,
  selected,
  onToggle,
  onToggleSkip,
}: {
  chapter: Chapter
  selected: boolean
  onToggle: () => void
  onToggleSkip: () => void
}) {
  const isSkipped = chapter.status === 'skipped'
  return (
    <tr className={`border-b last:border-b-0 ${isSkipped ? 'opacity-60' : ''}`}>
      <td className="px-3 py-2 align-top w-10">
        {!isSkipped && (
          <Checkbox checked={selected} onCheckedChange={onToggle} />
        )}
      </td>
      <td className="px-3 py-2 align-top w-20 font-mono text-xs">
        {chapter.renderable_index != null
          ? String(chapter.renderable_index).padStart(3, '0')
          : `b${chapter.block_index}`}
      </td>
      <td className="px-3 py-2 align-top">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="text-xs font-mono text-muted-foreground truncate">
              {chapter.stem ?? '— skipped —'}
            </div>
            <div className="text-xs text-muted-foreground line-clamp-2 mt-0.5">
              {chapter.preview || <em className="italic">empty</em>}
            </div>
          </div>
          <div className="text-right text-xs whitespace-nowrap">
            <div className="text-muted-foreground">{chapter.char_count} ch</div>
            {chapter.duration_s != null && (
              <div className="font-mono">{formatChapterTime(chapter.duration_s)}</div>
            )}
          </div>
        </div>
        {chapter.audio_url && (
          <audio
            controls
            src={chapter.audio_url}
            preload="none"
            className="w-full h-8 mt-2"
          />
        )}
      </td>
      <td className="px-3 py-2 align-top w-32 text-right">
        <div className="flex items-center justify-end gap-2">
          <ChapterStatusBadge status={chapter.status} />
          <Button
            size="sm"
            variant="ghost"
            className="h-7 w-7 p-0"
            onClick={onToggleSkip}
            title={isSkipped ? 'Unskip — include in render' : 'Skip — exclude from render'}
          >
            {isSkipped ? <Undo2 className="h-3 w-3" /> : <Ban className="h-3 w-3" />}
          </Button>
        </div>
      </td>
    </tr>
  )
}

function ChapterStatusBadge({ status }: { status: Chapter['status'] }) {
  switch (status) {
    case 'rendered':
      return <Badge variant="default" className="font-normal">rendered</Badge>
    case 'rendering':
      return <Badge variant="secondary" className="font-normal">rendering…</Badge>
    case 'failed':
      return <Badge variant="destructive" className="font-normal">failed</Badge>
    case 'skipped':
      return <Badge variant="outline" className="font-normal">skipped</Badge>
    case 'pending':
    default:
      return <Badge variant="outline" className="font-normal">pending</Badge>
  }
}
