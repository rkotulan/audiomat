import { useEffect, useMemo, useRef, useState } from 'react'
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
  Pencil,
  Mic,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Progress } from '@/components/ui/progress'
import { Badge } from '@/components/ui/badge'
import { ChapterTextEditor } from '@/components/ChapterTextEditor'
import { InlineModelProgress } from '@/components/InlineModelProgress'
import { InlineProgressCard } from '@/components/InlineProgressCard'
import { PronunciationsCard } from '@/components/PronunciationsCard'
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
import { useConfirm } from '@/components/ConfirmDialog'
import {
  buildM4b,
  cancelRender,
  deleteProject,
  getProject,
  listChapters,
  listModels,
  listVoices,
  previewCustom,
  previewMatrix,
  previewVoices,
  ProjectVersionConflict,
  projectM4bUrl,
  resetAllChapters,
  resetChapter,
  startRender,
  subscribeProgress,
  updateBlocksSkipped,
  updateProjectBook,
  updateProjectParams,
  updateProjectTtsModel,
  updateProjectVoice,
} from '@/lib/api'
import { LANGUAGE_OPTIONS, isValidLanguageCode } from '@/lib/languages'
import { capsForProject, formatParam, hasPresetMatrix } from '@/lib/caps'
import type {
  Chapter,
  ChaptersResponse,
  CustomPreviewResult,
  PreviewMatrix,
  PreviewVoicesMatrix,
  Project,
  ProgressEvent,
  TTSCapabilities,
  TTSModel,
  Voice,
  VoicePreviewCell,
} from '@/lib/types'

export function ProjectDetail() {
  const { slug = '' } = useParams()
  const nav = useNavigate()

  const [project, setProject] = useState<Project | null>(null)
  const [tab, setTab] = useState<string>('overview')
  const [err, setErr] = useState('')
  // Set when a PATCH was rejected with 409 (project was edited in another
  // tab). Banner-driven UX: user clicks Reload to fetch the new state and
  // can retry their edit. We deliberately don't auto-refresh — the user's
  // unsaved form values would be lost without warning.
  const [versionConflict, setVersionConflict] = useState(false)
  const [busy, setBusy] = useState(false)
  const [renderEvents, setRenderEvents] = useState<ProgressEvent[]>([])
  const [latest, setLatest] = useState<ProgressEvent | null>(null)
  const [chapters, setChapters] = useState<ChaptersResponse | null>(null)
  const [m4bPercent, setM4bPercent] = useState<number | null>(null)
  const [selectedIndices, setSelectedIndices] = useState<Set<number>>(new Set())
  // Stem of the chapter currently open in the text editor modal, or null
  // when the modal is closed.
  const [editingStem, setEditingStem] = useState<string | null>(null)
  const unsubRef = useRef<(() => void) | null>(null)
  const { confirm, dialog: confirmDialog } = useConfirm()
  // v0.5: TTS model registry — drives the Progress badges, render
  // confirmation copy, and the param matrix suppression in PreviewTab.
  // Lifted from PreviewTab so we don't refetch in multiple places.
  // `models[]` is small (≤ ~10 entries) and stable across project edits;
  // we refresh once on mount.
  const [models, setModels] = useState<TTSModel[]>([])
  useEffect(() => {
    listModels()
      .then(setModels)
      .catch(() => setModels([]))
  }, [])
  const projectCaps: TTSCapabilities | null = useMemo(() => {
    if (!project || models.length === 0) return null
    return capsForProject(project, models)
  }, [project, models])

  // Render-time ETA tracking. renderStart is the wall-clock ms at job
  // start; `now` ticks every second while busy so ETA refreshes live.
  // renderScope captures which 1-based renderable indices the current job
  // covers (null = all renderable). synthChars / synthSeconds accumulate
  // ONLY from chunk_synthed events (pure model.generate work — no setup
  // overhead, no cache hits) → rate is realistic from chunk #1.
  // seenChars adds chunk_cached on top so "chars done" includes everything
  // the worker has already processed, even cache hits.
  const [renderStart, setRenderStart] = useState<number | null>(null)
  const [renderScope, setRenderScope] = useState<Set<number> | null>(null)
  const [synthChars, setSynthChars] = useState(0)
  const [synthSeconds, setSynthSeconds] = useState(0)
  const [seenChars, setSeenChars] = useState(0)
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!busy) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [busy])

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
    setRenderStart(Date.now())
    setNow(Date.now())
    setRenderScope(indices ? new Set(indices) : null)
    setSynthChars(0)
    setSynthSeconds(0)
    setSeenChars(0)
    try {
      await startRender(slug, indices)
      unsubRef.current = subscribeProgress(slug, (e) => {
        setLatest(e)
        setRenderEvents((prev) => [...prev.slice(-200), e])
        // Per-chunk accumulators feed the rate/ETA calc. Synthed chunks
        // count toward both work-done AND throughput; cached chunks are
        // free, so they only count toward work-done (seenChars).
        if (e.kind === 'chunk_synthed') {
          setSynthChars((c) => c + (e.text_chars ?? 0))
          setSynthSeconds((s) => s + (e.gen_seconds ?? 0))
          setSeenChars((c) => c + (e.text_chars ?? 0))
        } else if (e.kind === 'chunk_cached') {
          setSeenChars((c) => c + (e.text_chars ?? 0))
        }
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

  const onResetAllChapters = () => {
    const renderedCount = chapters?.rendered_count ?? 0
    confirm({
      title: `Reset all ${renderedCount} rendered chapters?`,
      description:
        'Deletes every per-chapter cache under chunks/. previews/ and the final M4B stay. Use after voice / params / language changes that the manifest hash doesn\'t auto-detect. You\'ll need to render again from scratch.',
      confirmText: 'Reset all',
      destructive: true,
      onConfirm: async () => {
        try {
          await resetAllChapters(slug)
          await refreshChapters()
          refresh()
        } catch (e) {
          setErr(String(e))
        }
      },
    })
  }

  const onBuildM4b = async () => {
    setBusy(true)
    setErr('')
    setM4bPercent(null)
    try {
      await buildM4b(slug, {
        onStarted: () => setM4bPercent(0),
        onProgress: (p) => setM4bPercent(p),
      })
      refresh()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
      setM4bPercent(null)
    }
  }

  const onDelete = () => {
    confirm({
      title: `Delete project "${project?.name}"?`,
      description: 'This permanently removes the project directory — chunks, manifests, final M4B, render log. Cannot be undone.',
      confirmText: 'Delete project',
      destructive: true,
      onConfirm: async () => {
        try {
          await deleteProject(slug)
          nav('/projects')
        } catch (e) {
          setErr(String(e))
        }
      },
    })
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

  // Live render stats — chunk-level accumulators scoped to the current job.
  //   rate = synthChars / synthSeconds  (pure model.generate throughput)
  //   ETA  = (totalChars - seenChars) / rate
  // synthSeconds skips setup overhead (model load, EPUB parse, HF cache
  // checks) and cache-hit chapters, so the rate is realistic from the
  // first synthesized chunk. Updates per chunk_synthed event AND every
  // 1 s via the now ticker.
  const renderStats = (() => {
    if (!busy || renderStart == null || !chapters) return null
    const elapsed = (now - renderStart) / 1000

    // Scope to the active job's chapters. renderScope=null = Render all.
    const inScope = chapters.chapters.filter((c) => {
      if (c.renderable_index == null) return false
      if (renderScope === null) return true
      return renderScope.has(c.renderable_index)
    })
    const totalChars = inScope.reduce((sum, c) => sum + c.char_count, 0)
    const doneInScope = inScope.filter((c) => c.status === 'rendered').length

    // Need a reasonable amount of synth work before quoting a rate. Below
    // ~1 s of model.generate or ~150 chars synthed (one chunk-ish), the
    // ratio is dominated by per-chunk fixed overhead and gives misleading
    // numbers. Show "measuring…" until the floor is crossed.
    const haveEnoughData = synthSeconds > 1.5 && synthChars > 150
    const rate = haveEnoughData ? synthChars / synthSeconds : 0
    const remainingChars = Math.max(0, totalChars - seenChars)
    const eta =
      haveEnoughData && rate > 0 && remainingChars > 0
        ? remainingChars / rate
        : null

    return {
      elapsed,
      eta,
      rate,
      scopeCount: inScope.length,
      doneCount: doneInScope,
      seenChars,
      totalChars,
    }
  })()

  return (
    <div className="space-y-6">
      {confirmDialog}
      <ChapterTextEditor
        open={editingStem != null}
        slug={slug}
        stem={editingStem}
        onClose={() => setEditingStem(null)}
        onSaved={() => {
          // Refresh the chapter list so has_override + status update.
          refreshChapters()
        }}
      />

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

      {versionConflict && (
        <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm flex items-center justify-between gap-3">
          <div>
            <p className="font-medium text-amber-700 dark:text-amber-300">
              Project was edited in another tab
            </p>
            <p className="text-xs text-amber-700/80 dark:text-amber-300/80">
              Your last change was rejected to avoid overwriting the
              other edit. Reload to see the current state, then retry.
            </p>
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              setVersionConflict(false)
              refresh()
            }}
          >
            <RotateCw className="h-3 w-3" />
            Reload
          </Button>
        </div>
      )}

      {err && (
        <div className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
          {err}
        </div>
      )}

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="voice">Voice</TabsTrigger>
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
            <Button onClick={() => setTab('voice')}>
              Next: pick voice
              <ArrowRight className="h-4 w-4" />
            </Button>
          </div>
        </TabsContent>

        <TabsContent value="voice" className="space-y-4 pt-4">
          <VoiceTab
            project={project}
            slug={slug}
            onApply={refresh}
            onPicked={() => setTab('preview')}
            onVersionConflict={() => setVersionConflict(true)}
          />
        </TabsContent>

        <TabsContent value="preview" className="space-y-4 pt-4">
          <PreviewTab
            project={project}
            slug={slug}
            onApply={refresh}
            onPicked={() => setTab('render')}
            onVersionConflict={() => setVersionConflict(true)}
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
                {renderStats && (
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground font-mono">
                    <span>
                      job: {renderStats.doneCount}/{renderStats.scopeCount}
                      {renderScope === null ? ' (all)' : ' selected'}
                    </span>
                    <span>elapsed {formatChapterTime(renderStats.elapsed)}</span>
                    {renderStats.eta != null && (
                      <span>ETA {formatDuration(renderStats.eta)}</span>
                    )}
                    {renderStats.rate > 0 && (
                      <span>{Math.round(renderStats.rate)} chars/s</span>
                    )}
                    {renderStats.eta == null && renderStats.rate === 0 && (
                      <span className="italic">measuring…</span>
                    )}
                  </div>
                )}
              </div>

              {/* v0.5: badges loop over the active engine's declared
                  param specs. Engines with no params (Higgs) just show
                  the voice + engine badge — no stale OmniVoice knobs. */}
              <div className="flex flex-wrap gap-1.5">
                <Badge variant="secondary" className="font-normal">
                  <span className="text-muted-foreground mr-1">voice</span>
                  {project.voice_ref}
                </Badge>
                {projectCaps && (
                  <Badge variant="secondary" className="font-normal">
                    <span className="text-muted-foreground mr-1">engine</span>
                    {projectCaps.short_label}
                  </Badge>
                )}
                {projectCaps?.params.map((spec) => {
                  const value =
                    (project.params as unknown as Record<string, number>)[spec.name]
                  if (value == null) return null
                  return (
                    <Badge
                      key={spec.name}
                      variant="secondary"
                      className="font-normal font-mono"
                    >
                      <span className="text-muted-foreground mr-1 font-sans">
                        {spec.label}
                      </span>
                      {formatParam(spec, value)}
                    </Badge>
                  )
                })}
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

              {!busy && (chapters?.rendered_count ?? 0) > 0 && (
                <div className="flex items-center justify-between gap-3 pt-2 border-t border-dashed">
                  <p className="text-xs text-muted-foreground">
                    Wipes every cached chapter under <code>chunks/</code>.
                    Use to roll fresh diffusion noise across the whole book,
                    or to reclaim disk space after a render. Voice / params
                    changes auto-invalidate via the manifest signature — no
                    manual wipe needed for those.
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-destructive hover:text-destructive hover:bg-destructive/10 shrink-0"
                    onClick={onResetAllChapters}
                  >
                    <Trash2 className="h-4 w-4" />
                    Reset all chapters
                  </Button>
                </div>
              )}

              <InlineModelProgress visible={busy} />

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
                await updateBlocksSkipped(slug, next, project.version)
                await refreshChapters()
                refresh()
              } catch (e) {
                if (e instanceof ProjectVersionConflict) {
                  setVersionConflict(true)
                } else {
                  alert(`Failed to update skip list: ${e}`)
                }
              }
            }}
            onResetChapter={(stem, renderableIndex) => {
              confirm({
                title: `Reset chapter ${stem}?`,
                description: 'Deletes the cached chunks + final WAV for this chapter. Status flips to pending; click Render selected to re-synth.',
                confirmText: 'Reset',
                destructive: true,
                onConfirm: async () => {
                  try {
                    await resetChapter(slug, stem)
                    await refreshChapters()
                    if (renderableIndex != null) {
                      setSelectedIndices(new Set([renderableIndex]))
                    }
                  } catch (e) {
                    setErr(`Reset failed: ${e}`)
                  }
                },
              })
            }}
            onEditChapter={(stem) => setEditingStem(stem)}
          />
        </TabsContent>

        <TabsContent value="output" className="space-y-4 pt-4">
          <Card>
            <CardHeader>
              <CardTitle>M4B</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {chapters && chapters.renderable_total > 0 && (
                <div className="rounded-md bg-secondary/40 p-3 text-sm space-y-1">
                  <p className="font-medium">
                    {chapters.rendered_count} / {chapters.renderable_total} chapters rendered
                    {chapters.rendered_count === chapters.renderable_total ? ' ✓' : ''}
                  </p>
                  {chapters.rendered_count < chapters.renderable_total && (
                    <p className="text-muted-foreground text-xs">
                      Partial M4B is OK — you'll get an audiobook with whatever's
                      currently rendered. Build again later to include new chapters.
                    </p>
                  )}
                </div>
              )}

              {project.has_final_m4b ? (
                <>
                  <p className="text-sm text-muted-foreground">
                    M4B already built. Download below, or Rebuild after rendering
                    more chapters / changing voice.
                  </p>
                  <div className="flex gap-2">
                    <Button asChild>
                      <a href={projectM4bUrl(slug)} download>
                        <Download className="h-4 w-4" />
                        Download M4B
                      </a>
                    </Button>
                    <Button
                      variant="outline"
                      onClick={onBuildM4b}
                      disabled={busy || (chapters?.rendered_count ?? 0) === 0}
                    >
                      <Hammer className="h-4 w-4" />
                      {busy ? 'Building…' : 'Rebuild M4B'}
                    </Button>
                  </div>
                  {busy && m4bPercent != null && (
                    <InlineProgressCard
                      message="Building M4B…"
                      percent={m4bPercent}
                    />
                  )}
                </>
              ) : (
                <>
                  <p className="text-sm text-muted-foreground">
                    Concatenates per-chapter WAVs into a single M4B with chapter
                    markers + ID3 metadata. Available as soon as one chapter
                    finishes rendering.
                  </p>
                  <Button
                    onClick={onBuildM4b}
                    disabled={busy || (chapters?.rendered_count ?? 0) === 0}
                  >
                    <Hammer className="h-4 w-4" />
                    {busy
                      ? 'Building…'
                      : chapters && chapters.rendered_count > 0
                      ? `Build M4B (${chapters.rendered_count}/${chapters.renderable_total})`
                      : 'Build M4B'}
                  </Button>
                  {busy && m4bPercent != null && (
                    <InlineProgressCard
                      message="Building M4B…"
                      percent={m4bPercent}
                    />
                  )}
                </>
              )}
            </CardContent>
          </Card>

          {project.has_final_m4b && (chapters?.rendered_count ?? 0) > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Free chapter cache</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <p className="text-sm text-muted-foreground">
                  Your M4B is built. Wipe the per-chapter cache under{' '}
                  <code>chunks/</code> to reclaim disk space — the M4B itself
                  stays. If you change params or rebuild later, just hit
                  Render again and the chapters re-synth automatically.
                </p>
                <Button
                  variant="outline"
                  onClick={onResetAllChapters}
                  disabled={busy}
                >
                  <Trash2 className="h-4 w-4" />
                  Free cache ({chapters?.rendered_count ?? 0} chapters)
                </Button>
              </CardContent>
            </Card>
          )}
        </TabsContent>

        <TabsContent value="advanced" className="space-y-4 pt-4">
          <TTSEngineCard
            project={project}
            models={models}
            slug={slug}
            onSaved={refresh}
            onVersionConflict={() => setVersionConflict(true)}
          />
          <OutputParamsCard
            project={project}
            slug={slug}
            onSaved={refresh}
            onVersionConflict={() => setVersionConflict(true)}
          />
          <BookMetadataCard
            project={project}
            slug={slug}
            onSaved={refresh}
            onVersionConflict={() => setVersionConflict(true)}
          />
          <PronunciationsCard slug={slug} />
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
 * gen_seconds is 0 only when the cell was generated before the
 * previews/_gen_times.json sidecar existed (legacy entries) — in that
 * case we can't extrapolate and the UI nudges the user to re-tune.
 * For cells with a sidecar entry, cached vs fresh doesn't matter:
 * gen_seconds is the same number either way.
 */
function estimateBookRender(
  matrix: PreviewMatrix,
  variant: PreviewMatrix['variants'][number],
): string {
  if (variant.gen_seconds <= 0) {
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
  onVersionConflict,
}: {
  project: Project
  slug: string
  onApply: () => void
  onPicked: () => void
  onVersionConflict: () => void
}) {
  const [matrix, setMatrix] = useState<PreviewMatrix | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [tuningIndex, setTuningIndex] = useState<number | null>(null)
  const [tunedFlags, setTunedFlags] = useState<Set<number>>(new Set())
  const [cellsDone, setCellsDone] = useState(0)
  const [cellsTotal, setCellsTotal] = useState(0)

  // v0.5: capabilities-driven. The active engine's preset_variants
  // determines whether the matrix UI even makes sense; engines with
  // <2 presets (Higgs ships zero — autoregressive LM with no knobs)
  // get the "skip" explainer instead of 4 identical-sounding cells.
  // No more `backend === 'higgs'` branches.
  const [models, setModels] = useState<TTSModel[]>([])
  useEffect(() => {
    listModels()
      .then(setModels)
      .catch(() => setModels([]))
  }, [])
  const projectCaps: TTSCapabilities | null = useMemo(() => {
    if (models.length === 0) return null
    return capsForProject(project, models)
  }, [project, models])

  const onGenerate = async () => {
    setBusy(true)
    setErr('')
    setCellsDone(0)
    setCellsTotal(0)
    try {
      const m = await previewMatrix(slug, {
        onStarted: (h) => setCellsTotal(h.total),
        onCellDone: (idx) => setCellsDone(idx + 1),
      })
      setMatrix(m)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
      setCellsDone(0)
      setCellsTotal(0)
    }
  }

  const onPick = async (variant: PreviewMatrix['variants'][number]) => {
    try {
      await updateProjectParams(slug, {
        num_step: variant.num_step,
        guidance_scale: variant.guidance_scale,
        speed: variant.speed,
      }, project.version)
      onApply()
      onPicked()
    } catch (e) {
      if (e instanceof ProjectVersionConflict) {
        onVersionConflict()
      } else {
        alert(`Failed to apply params: ${e}`)
      }
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

  // v0.5: engines with no preset matrix (Higgs is the canonical case —
  // autoregressive LM with no diffusion knobs) get the "skip" explainer
  // instead of N cells that all sound the same modulo stochastic
  // sampling. `hasPresetMatrix` returns false for any engine missing
  // either ≥2 variants or ≥1 tunable param — purely capability-driven.
  // The project's stored RenderParams are kept (in case the user later
  // swaps back to OmniVoice they don't lose the tuned values), they
  // just don't drive Higgs render time.
  if (projectCaps && !hasPresetMatrix(projectCaps)) {
    return (
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle>
              Parameter matrix (skipped — engine has no tunable params)
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            <p>
              The parameter matrix A/Bs an engine's render knobs. The
              current engine ({projectCaps.display_name}) doesn't expose
              any — rendering N cells would just burn time on samples
              that all sound the same modulo stochastic sampling.
            </p>
            <p>
              To hear what this voice sounds like on the book's sample
              text, use the <strong>Voice</strong> tab — its picker
              matrix renders one cell per voice with the project's
              actual sample. To start the full book render, jump to{' '}
              <strong>Render</strong>.
            </p>
            <div className="flex gap-2">
              <Button onClick={onPicked}>
                <ArrowRight className="h-4 w-4" />
                Skip to Render
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    )
  }

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
            First call: ~22 s on OmniVoice (RTF ~0.25) or ~90 s on Higgs
            (RTF ~0.8) — sequential inference across 4 cells against
            the project's bound TTS backend. Subsequent calls re-use
            the cache.
          </p>
          <div className="flex gap-2">
            <Button onClick={onGenerate} disabled={busy}>
              <Wand2 className="h-4 w-4" />
              {busy ? 'Generating…' : matrix ? 'Re-generate' : 'Generate matrix'}
            </Button>
          </div>
          <InlineModelProgress visible={busy} />
          {busy && cellsTotal > 0 && (
            <InlineProgressCard
              message={`Generating ${cellsDone} / ${cellsTotal} cells…`}
              percent={(cellsDone / cellsTotal) * 100}
            />
          )}
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
                    {(v.tuned || tunedFlags.has(idx)) && (
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
        caps={projectCaps}
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
// Voice tab — pick which library voice to use for this project. User
// selects any subset of voices, hits Generate, hears the same project
// sample rendered by each, and clicks "Use this voice" to commit.
// Voice swap invalidates the chunk cache automatically (manifest
// signature includes the voice slug), so a swap mid-project just
// re-renders on next run.
// ----------------------------------------------------------------------------

// Default seed for the checklist — small enough to be actionable
// without "select all and wait", large enough to be a useful first A/B.
// Soft default only; the checklist has no hard cap.
const DEFAULT_VOICES_IN_MATRIX = 4

function VoiceTab({
  project, slug, onApply, onPicked, onVersionConflict,
}: {
  project: Project
  slug: string
  onApply: () => void
  onPicked: () => void
  onVersionConflict: () => void
}) {
  const [voices, setVoices] = useState<Voice[] | null>(null)
  const [models, setModels] = useState<TTSModel[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [matrix, setMatrix] = useState<PreviewVoicesMatrix | null>(null)
  const [busy, setBusy] = useState(false)
  const [committing, setCommitting] = useState<string | null>(null)
  const [err, setErr] = useState('')
  const [cellsDone, setCellsDone] = useState(0)
  const [cellsTotal, setCellsTotal] = useState(0)

  // Look up which model (and therefore which license) backs a voice.
  // Cells with non-commercial backing models get a warning badge so
  // the user knows what they're about to commit the project to.
  const modelBySlug = useMemo(() => {
    const map = new Map<string, TTSModel>()
    for (const m of models) map.set(m.name_slug, m)
    return map
  }, [models])

  const licenseForVoice = (voiceSlug: string): 'permissive' | 'non_commercial' | null => {
    const v = voices?.find((x) => x.name_slug === voiceSlug)
    if (!v || !v.tts_model) return null
    const m = modelBySlug.get(v.tts_model)
    return m ? m.license : null
  }

  // v0.5: project's active engine caps drive the data-driven render
  // confirmation text. If models haven't loaded yet, the explainer
  // omits the per-param breakdown.
  const projectCaps: TTSCapabilities | null = useMemo(() => {
    if (models.length === 0) return null
    return capsForProject(project, models)
  }, [project, models])

  // Load voice library + models and seed selection: project's current
  // voice + up to 3 most recent (by created date desc). User can re-tick.
  useEffect(() => {
    Promise.all([listVoices(), listModels().catch(() => [])])
      .then(([vs, ms]) => {
        setVoices(vs)
        setModels(ms)
        const seed = new Set<string>()
        if (project.voice_ref_slug) seed.add(project.voice_ref_slug)
        const sorted = [...vs].sort((a, b) => b.created.localeCompare(a.created))
        for (const v of sorted) {
          if (seed.size >= DEFAULT_VOICES_IN_MATRIX) break
          seed.add(v.name_slug)
        }
        setSelected(seed)
      })
      .catch((e) => setErr(String(e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggleVoice = (slug: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(slug)) {
        next.delete(slug)
      } else {
        next.add(slug)
      }
      return next
    })
  }

  const onGenerate = async () => {
    if (selected.size === 0) return
    setBusy(true)
    setErr('')
    setMatrix(null)
    setCellsDone(0)
    setCellsTotal(0)
    try {
      const m = await previewVoices(slug, [...selected], {
        onStarted: (h) => setCellsTotal(h.total),
        onCellDone: (idx) => setCellsDone(idx + 1),
      })
      setMatrix(m)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
      setCellsDone(0)
      setCellsTotal(0)
    }
  }

  const onUseVoice = async (cell: VoicePreviewCell) => {
    setCommitting(cell.voice_slug)
    setErr('')
    try {
      await updateProjectVoice(slug, cell.voice_slug, project.version)
      onApply()
      onPicked()
    } catch (e) {
      if (e instanceof ProjectVersionConflict) {
        onVersionConflict()
      } else {
        setErr(String(e))
      }
    } finally {
      setCommitting(null)
    }
  }

  const isCurrent = (cell: VoicePreviewCell) =>
    cell.voice_slug === project.voice_ref_slug

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Voice picker</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Pick voices from your library and hear the same book sample
            rendered by each. Listen, then click <em>Use this voice</em> on
            whichever fits the book best.
          </p>
          <p className="text-xs text-muted-foreground">
            {projectCaps && projectCaps.params.length > 0 ? (
              <>
                Render uses the project's current params (
                {projectCaps.params.map((spec, i) => {
                  const value =
                    (project.params as unknown as Record<string, number>)[spec.name]
                  return (
                    <span key={spec.name}>
                      {i > 0 ? ', ' : ''}
                      {spec.label}{' '}
                      {value != null ? formatParam(spec, value) : '—'}
                    </span>
                  )
                })}
                ).{' '}
              </>
            ) : projectCaps ? (
              <>Render uses {projectCaps.display_name}. </>
            ) : null}
            Swapping voice invalidates rendered chunks via the manifest
            signature — a swap mid-project just re-renders on next run.
          </p>

          {voices === null ? (
            <p className="text-sm text-muted-foreground">Loading voices…</p>
          ) : voices.length === 0 ? (
            <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              No voices in library yet.{' '}
              <Link to="/voices/new" className="text-primary underline">
                Add one first
              </Link>
              .
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label className="text-xs uppercase tracking-wide text-muted-foreground">
                  Voices to compare ({selected.size} of {voices.length})
                </Label>
                <div className="flex gap-2 text-xs">
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-foreground underline"
                    onClick={() => setSelected(new Set(voices.map((v) => v.name_slug)))}
                  >
                    Select all
                  </button>
                  <span className="text-muted-foreground">·</span>
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-foreground underline"
                    onClick={() => setSelected(
                      project.voice_ref_slug
                        ? new Set([project.voice_ref_slug])
                        : new Set(),
                    )}
                  >
                    Clear
                  </button>
                </div>
              </div>
              <div className="grid gap-2 md:grid-cols-2">
                {voices.map((v) => {
                  const isSel = selected.has(v.name_slug)
                  return (
                    <label
                      key={v.name_slug}
                      className={[
                        'flex items-center gap-2 rounded-md border p-2 text-sm cursor-pointer transition-colors',
                        isSel ? 'border-primary bg-primary/5' : 'hover:bg-secondary/40',
                      ].join(' ')}
                    >
                      <Checkbox
                        checked={isSel}
                        onCheckedChange={() => toggleVoice(v.name_slug)}
                      />
                      <span className="flex-1 truncate">{v.name}</span>
                      {licenseForVoice(v.name_slug) === 'non_commercial' && (
                        <Badge
                          variant="outline"
                          className="font-normal border-amber-500/60 text-amber-700 dark:text-amber-300 bg-amber-500/10 text-xs"
                          title="Voice uses a non-commercial model"
                        >
                          NC
                        </Badge>
                      )}
                      {v.name_slug === project.voice_ref_slug && (
                        <Badge variant="default" className="font-normal">
                          <Star className="h-3 w-3" /> current
                        </Badge>
                      )}
                      <span className="text-xs text-muted-foreground font-mono shrink-0">
                        {v.duration_s.toFixed(1)} s
                      </span>
                    </label>
                  )
                })}
              </div>
            </div>
          )}

          <div className="flex gap-2">
            <Button
              onClick={onGenerate}
              disabled={busy || selected.size === 0 || voices === null}
            >
              <Mic className="h-4 w-4" />
              {busy
                ? 'Generating…'
                : matrix
                ? 'Re-generate'
                : `Generate matrix (${selected.size} ${selected.size === 1 ? 'voice' : 'voices'})`}
            </Button>
          </div>

          <InlineModelProgress visible={busy} />
          {busy && cellsTotal > 0 && (
            <InlineProgressCard
              message={`Generating ${cellsDone} / ${cellsTotal} voices…`}
              percent={(cellsDone / cellsTotal) * 100}
            />
          )}
          {err && <div className="text-sm text-destructive">{err}</div>}

          {matrix && (
            <div className="rounded-md border bg-muted/40 p-3 text-xs space-y-1">
              <p className="font-medium">
                Sample text — block {matrix.sample_block_index + 1} of{' '}
                {matrix.sample_block_total} ({matrix.sample_chars} chars)
              </p>
              <p className="italic line-clamp-3">{matrix.sample_text}</p>
              <p className="text-muted-foreground">
                Same picker as the Preview tab uses, so you'll hear identical
                text in both.
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      {matrix && (
        <div className="grid gap-4 md:grid-cols-2">
          {matrix.voices.map((cell) => (
            <Card
              key={cell.voice_slug}
              className={isCurrent(cell) ? 'border-primary' : ''}
            >
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center justify-between">
                  <span className="flex items-center gap-2">
                    {cell.voice_name}
                    {licenseForVoice(cell.voice_slug) === 'non_commercial' && (
                      <Badge
                        variant="outline"
                        className="font-normal border-amber-500/60 text-amber-700 dark:text-amber-300 bg-amber-500/10"
                        title="Rendered via a non-commercial model"
                      >
                        non-commercial
                      </Badge>
                    )}
                    {isCurrent(cell) && (
                      <Badge variant="default" className="font-normal">
                        <Star className="h-3 w-3" /> current
                      </Badge>
                    )}
                  </span>
                  <span className="text-xs text-muted-foreground font-normal font-mono">
                    {cell.duration_s.toFixed(1)} s
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <audio
                  controls
                  src={cell.audio_url}
                  preload="metadata"
                  className="w-full h-9"
                />
                <div className="flex items-center justify-between gap-2">
                  <span className="text-xs text-muted-foreground">
                    {cell.cached ? 'cached' : `${cell.gen_seconds.toFixed(1)} s sample`}
                  </span>
                  <Button
                    size="sm"
                    onClick={() => onUseVoice(cell)}
                    disabled={
                      committing !== null ||
                      isCurrent(cell)
                    }
                  >
                    {committing === cell.voice_slug
                      ? 'Switching…'
                      : isCurrent(cell)
                      ? 'In use'
                      : (
                        <>
                          Use this voice
                          <ArrowRight className="h-3 w-3" />
                        </>
                      )}
                  </Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
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
  caps,
  slug,
  onClose,
  onTuned,
}: {
  open: boolean
  variant:
    | (PreviewMatrix['variants'][number])
    | null
  // v0.5: engine's declared param specs drive the slider rows
  // (label / hint / range / default / formatting). Dialog never opens
  // for engines with empty caps.params — `hasPresetMatrix` gates the
  // matrix UI upstream.
  caps: TTSCapabilities | null
  slug: string
  onClose: () => void
  onTuned: (custom: CustomPreviewResult) => void
}) {
  // params is a free-form dict keyed by spec name. Seeded from the
  // variant's wire fields (still typed as the OmniVoice shape over the
  // SSE channel in v0.5) or, when the variant lacks a key, the spec
  // default.
  const seedParams = (): Record<string, number> => {
    const out: Record<string, number> = {}
    if (!caps) return out
    const variantAny = (variant as unknown as Record<string, number>) ?? {}
    for (const spec of caps.params) {
      out[spec.name] =
        variantAny[spec.name] != null ? variantAny[spec.name] : spec.default
    }
    return out
  }
  const [params, setParams] = useState<Record<string, number>>(seedParams())
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  // Re-init local sliders whenever the dialog opens for a different
  // variant or the active caps change (engine swap on the parent).
  useEffect(() => {
    setParams(seedParams())
    setErr('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variant, caps])

  const onGenerate = async () => {
    setBusy(true)
    setErr('')
    try {
      // previewCustom's v0.4 body shape is still typed as
      // {num_step, guidance_scale, speed} — for OmniVoice (the only
      // engine with a matrix today) those keys are present in `params`
      // because we seeded from the spec list. Cast through unknown to
      // ship the dict as-is; downstream Pydantic ignores unknown keys.
      const result = await previewCustom(
        slug,
        {
          ...(params as unknown as {
            num_step: number
            guidance_scale: number
            speed: number
          }),
          // Tag with the matrix cell label so the backend persists this
          // tuning into previews/_tuned_cells.json — the matrix will
          // restore it on the next render instead of falling back to
          // the preset.
          label: variant?.label,
        },
      )
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
          {caps?.params.map((spec) => (
            <SliderRow
              key={spec.name}
              label={spec.label}
              hint={spec.hint}
              value={params[spec.name] ?? spec.default}
              min={spec.min}
              max={spec.max}
              step={spec.step}
              format={(v) => formatParam(spec, v)}
              onChange={(v) =>
                setParams((prev) => ({ ...prev, [spec.name]: v }))
              }
            />
          ))}
        </div>

        <InlineModelProgress visible={busy} />
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

// ----------------------------------------------------------------------------
// TTSEngineCard — v0.5. Project-level engine picker. The matrix-cell
// labels, sliders, license badges, etc. throughout the rest of the UI
// react to whichever engine this card sets. Engine swap invalidates
// rendered chunks via the manifest signature (see render._params_signature).
// ----------------------------------------------------------------------------

const DEFAULT_SLUG = 'default'

function TTSEngineCard({
  project,
  models,
  slug,
  onSaved,
  onVersionConflict,
}: {
  project: Project
  models: TTSModel[]
  slug: string
  onSaved: () => void
  onVersionConflict: () => void
}) {
  // Project stores null for "stock OmniVoice"; the <select> value is
  // the wire-level `"default"` token (`DEFAULT_MODEL_SLUG`) so it can
  // round-trip cleanly through the dropdown.
  const current = project.tts_model || DEFAULT_SLUG
  const [selected, setSelected] = useState<string>(current)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  // Keep selected in sync if the project gets refetched (e.g. after a
  // voice swap PATCH refreshes the project payload).
  useEffect(() => {
    setSelected(project.tts_model || DEFAULT_SLUG)
  }, [project.tts_model])

  const dirty = selected !== current
  const selectedModel = models.find((m) => m.name_slug === selected) ?? null
  const selectedCaps = selectedModel?.capabilities ?? null

  const onSave = async () => {
    setSaving(true)
    setMsg('')
    try {
      // Send null over the wire for stock; backend also accepts
      // "default" but null avoids it bouncing back as a non-null
      // string in the response.
      const payload = selected === DEFAULT_SLUG ? null : selected
      await updateProjectTtsModel(slug, payload, project.version)
      setMsg(
        'Saved. Cached chunks rendered by the previous engine will be '
        + 're-synthesized on the next render.',
      )
      onSaved()
    } catch (e) {
      if (e instanceof ProjectVersionConflict) {
        onVersionConflict()
      } else {
        setMsg(`Failed: ${e}`)
      }
    } finally {
      setSaving(false)
    }
  }

  const onReset = () => {
    setSelected(current)
    setMsg('')
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>TTS engine</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="text-muted-foreground">
          Which TTS engine renders this project. Swapping engines
          invalidates the chunk cache via the manifest signature — a
          swap mid-project re-renders the affected chapters on next run.
        </p>

        <div className="space-y-2">
          <Label className="text-xs uppercase tracking-wide text-muted-foreground">
            Engine
          </Label>
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            className="w-full rounded-md border bg-background px-3 py-2 text-sm"
          >
            {models.length === 0 && (
              <option value={DEFAULT_SLUG}>Loading…</option>
            )}
            {models.map((m) => {
              const ncSuffix =
                m.license === 'non_commercial' ? ' · non-commercial' : ''
              return (
                <option key={m.name_slug} value={m.name_slug}>
                  {m.name} · {m.capabilities.short_label}{ncSuffix}
                </option>
              )
            })}
          </select>
        </div>

        {/* License hint — surfaces the same amber NC warning the
            voice picker already uses, but driven straight from caps
            instead of branching on backend === 'higgs'. */}
        {selectedModel?.license === 'non_commercial' && selectedCaps && (
          <p className="rounded-md border border-amber-400 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-100">
            Non-commercial license: {selectedCaps.license_name}. audiomat
            itself stays MIT, but renders produced with this engine carry
            its license obligations — review before commercial use.
          </p>
        )}

        {/* Capability hint — feature flags, typical RTF / VRAM so the
            user has something concrete before swapping. */}
        {selectedCaps && (
          <div className="grid grid-cols-2 gap-2 text-xs text-muted-foreground">
            <span>
              params{' '}
              <span className="font-mono text-foreground">
                {selectedCaps.params.length}
              </span>
            </span>
            <span>
              presets{' '}
              <span className="font-mono text-foreground">
                {selectedCaps.preset_variants.length}
              </span>
            </span>
            <span>
              typical RTF{' '}
              <span className="font-mono text-foreground">
                {selectedCaps.typical_rtf.toFixed(2)}
              </span>
            </span>
            <span>
              VRAM{' '}
              <span className="font-mono text-foreground">
                ~{selectedCaps.typical_vram_gb.toFixed(1)} GB
              </span>
            </span>
          </div>
        )}

        <div className="flex items-center justify-between pt-2">
          <span className="text-xs text-muted-foreground">{msg}</span>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onReset} disabled={!dirty || saving}>
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


function OutputParamsCard({
  project,
  slug,
  onSaved,
  onVersionConflict,
}: {
  project: Project
  slug: string
  onSaved: () => void
  onVersionConflict: () => void
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
      await updateProjectParams(slug, params, project.version)
      setMsg('Saved.')
      onSaved()
    } catch (e) {
      if (e instanceof ProjectVersionConflict) {
        onVersionConflict()
      } else {
        setMsg(`Failed: ${e}`)
      }
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

// ----------------------------------------------------------------------------
// BookMetadataCard — currently exposes the language tag only. Used to
// override mis-detected EPUB metadata or correct a TXT project that was
// created with the wrong default. Mounted in the Advanced tab.
// ----------------------------------------------------------------------------

function BookMetadataCard({
  project,
  slug,
  onSaved,
  onVersionConflict,
}: {
  project: Project
  slug: string
  onSaved: () => void
  onVersionConflict: () => void
}) {
  const initial = project.book.language || 'cs'
  const isPreset = LANGUAGE_OPTIONS.some((o) => o.code === initial)
  const [selected, setSelected] = useState<string>(isPreset ? initial : 'other')
  const [custom, setCustom] = useState<string>(isPreset ? '' : initial)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')

  const effective =
    selected === 'other' ? custom.trim().toLowerCase() : selected
  const isValid = isValidLanguageCode(effective)
  const dirty = effective !== initial

  const onSave = async () => {
    if (!isValid) return
    setSaving(true)
    setMsg('')
    try {
      await updateProjectBook(slug, { language: effective }, project.version)
      setMsg('Saved.')
      onSaved()
    } catch (e) {
      if (e instanceof ProjectVersionConflict) {
        onVersionConflict()
      } else {
        setMsg(`Failed: ${e}`)
      }
    } finally {
      setSaving(false)
    }
  }

  const onReset = () => {
    setSelected(isPreset ? initial : 'other')
    setCustom(isPreset ? '' : initial)
    setMsg('')
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Book metadata</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Override the language tag stored with this project. EPUB DC
          metadata is auto-detected on import; edit here if it was missing
          or wrong (e.g. an English book mis-tagged as <code>en-GB</code>{' '}
          when you want <code>en</code>). Affects number-to-text expansion
          (cs: <code>1959</code> → <code>tisíc devět set padesát devět</code>)
          and the language passed to OmniVoice.
        </p>

        <div className="space-y-2">
          <Label htmlFor="adv-lang">Language</Label>
          <select
            id="adv-lang"
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {LANGUAGE_OPTIONS.map((opt) => (
              <option key={opt.code} value={opt.code}>
                {opt.label}
              </option>
            ))}
            <option value="other">Other (custom code)…</option>
          </select>
          {selected === 'other' && (
            <Input
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              placeholder="ISO 639-1 / BCP 47 (e.g. ja, ko, pt-BR)"
            />
          )}
          <p className="text-xs text-muted-foreground">
            Current: <code className="font-mono">{initial}</code>
          </p>
          {!isValid && (
            <p className="text-xs text-destructive">
              Invalid code — use ISO 639-1 (cs, en) or BCP 47 (cs-CZ, pt-BR).
            </p>
          )}
        </div>

        <div className="flex items-center justify-between pt-2">
          <span className="text-xs text-muted-foreground">{msg}</span>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onReset} disabled={!dirty}>
              Reset
            </Button>
            <Button onClick={onSave} disabled={!dirty || !isValid || saving}>
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
  onResetChapter,
  onEditChapter,
}: {
  slug: string
  chapters: ChaptersResponse | null
  selectedIndices: Set<number>
  setSelectedIndices: (s: Set<number>) => void
  onRefresh: () => void
  onToggleSkip: (blockIndex: number, becomeSkipped: boolean) => void | Promise<void>
  onResetChapter: (stem: string, renderableIndex: number | null) => void | Promise<void>
  onEditChapter: (stem: string) => void
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
                  onResetChapter={() =>
                    c.stem && onResetChapter(c.stem, c.renderable_index)
                  }
                  onEditChapter={() => c.stem && onEditChapter(c.stem)}
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
  onResetChapter,
  onEditChapter,
}: {
  chapter: Chapter
  selected: boolean
  onToggle: () => void
  onToggleSkip: () => void
  onResetChapter: () => void
  onEditChapter: () => void
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
            <div className="flex items-center gap-1.5 text-xs font-mono text-muted-foreground truncate">
              <span className="truncate">{chapter.stem ?? '— skipped —'}</span>
              {chapter.has_override && (
                <Badge
                  variant="outline"
                  className="font-normal text-[10px] py-0 h-4 border-amber-500/50 text-amber-600 shrink-0"
                  title="This chapter's text has been edited — render uses override, not the EPUB original"
                >
                  edited
                </Badge>
              )}
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
      <td className="px-3 py-2 align-top w-40 text-right">
        <div className="flex items-center justify-end gap-1">
          <ChapterStatusBadge status={chapter.status} />
          {!isSkipped && chapter.stem && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 w-7 p-0"
              onClick={onEditChapter}
              title="Edit chapter text — typo fix, manual pauses, pronunciation tweak"
            >
              <Pencil className="h-3 w-3" />
            </Button>
          )}
          {chapter.status === 'rendered' && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 w-7 p-0"
              onClick={onResetChapter}
              title="Reset cache — wipes chunks/final.wav so next render re-synthesizes from scratch"
            >
              <RotateCw className="h-3 w-3" />
            </Button>
          )}
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
