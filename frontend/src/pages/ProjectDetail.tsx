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
  ChevronDown,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Progress } from '@/components/ui/progress'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import { Slider } from '@/components/ui/slider'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  buildM4b,
  deleteProject,
  getProject,
  previewCustom,
  previewMatrix,
  projectM4bUrl,
  startRender,
  subscribeProgress,
  updateProjectParams,
} from '@/lib/api'
import type {
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
  const unsubRef = useRef<(() => void) | null>(null)

  const refresh = () =>
    getProject(slug)
      .then(setProject)
      .catch((e) => setErr(String(e)))

  useEffect(() => {
    refresh()
    return () => unsubRef.current?.()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug])

  const onRender = async () => {
    setErr('')
    setBusy(true)
    setRenderEvents([])
    setLatest(null)
    try {
      await startRender(slug)
      unsubRef.current = subscribeProgress(slug, (e) => {
        setLatest(e)
        setRenderEvents((prev) => [...prev.slice(-200), e])
        if (e.kind === 'render_complete' || e.kind === 'error') {
          unsubRef.current?.()
          unsubRef.current = null
          setBusy(false)
          refresh()
        }
      })
    } catch (e) {
      setErr(String(e))
      setBusy(false)
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
                    Chapter {latest?.chapter_idx ?? project.status.chapters_done}/
                    {latest?.chapter_total ?? project.status.chapters_total}
                  </span>
                  <span className="text-muted-foreground">{pct}%</span>
                </div>
                <Progress value={pct} />
              </div>

              <div className="flex gap-2">
                <Button onClick={onRender} disabled={busy}>
                  <Play className="h-4 w-4" />
                  {busy ? 'Rendering…' : project.status.chapters_done > 0
                    ? 'Continue render'
                    : 'Start render'}
                </Button>
              </div>

              {renderEvents.length > 0 && (
                <div className="border rounded-md max-h-[300px] overflow-y-auto font-mono text-xs">
                  {renderEvents.slice().reverse().map((e, i) => (
                    <EventRow key={i} event={e} />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
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
          <Card>
            <CardHeader>
              <CardTitle className="text-destructive">Danger zone</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground mb-3">
                Render parameters live on the Preview tab (Custom params section).
              </p>
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

      <details className="rounded-md border bg-card group">
        <summary className="cursor-pointer select-none list-none px-4 py-3 flex items-center justify-between text-sm font-medium hover:bg-accent/50 transition-colors">
          <span className="flex items-center gap-2">
            <ChevronDown className="h-4 w-4 transition-transform group-open:rotate-180" />
            Custom params
          </span>
          <span className="text-xs text-muted-foreground font-mono">
            step {project.params.num_step} · gs {project.params.guidance_scale.toFixed(1)} ·{' '}
            speed {project.params.speed.toFixed(2)}×
          </span>
        </summary>
        <div className="border-t p-4">
          <CustomParamsSection project={project} slug={slug} onSaved={onApply} />
        </div>
      </details>

      {matrix && (
        <div className="grid gap-4 md:grid-cols-2">
          {matrix.variants.map((v) => (
            <Card key={v.label} className={isCurrent(v) ? 'border-primary' : ''}>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center justify-between">
                  <span className="flex items-center gap-2">
                    {v.label}
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
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span>{v.cached ? 'cached' : `${v.gen_seconds.toFixed(1)} s`}</span>
                  <Button size="sm" onClick={() => onPick(v)}>
                    Use this
                    <ArrowRight className="h-3 w-3" />
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
// Custom params — manual tuning panel embedded in the Preview tab
// (collapsible <details> wrapper). Power-user knobs that complement the
// 4-cell preset matrix above.
// ----------------------------------------------------------------------------

function CustomParamsSection({
  project,
  slug,
  onSaved,
}: {
  project: Project
  slug: string
  onSaved: () => void
}) {
  const [params, setParams] = useState({
    num_step: project.params.num_step,
    guidance_scale: project.params.guidance_scale,
    speed: project.params.speed,
    min_chars: project.params.min_chars,
    max_chars: project.params.max_chars,
    target_lufs: project.params.target_lufs,
    silence_gap_ms: project.params.silence_gap_ms,
  })
  const [saving, setSaving] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [previewResult, setPreviewResult] = useState<CustomPreviewResult | null>(null)
  const [msg, setMsg] = useState('')
  const dirty =
    params.num_step !== project.params.num_step ||
    params.guidance_scale !== project.params.guidance_scale ||
    params.speed !== project.params.speed ||
    params.min_chars !== project.params.min_chars ||
    params.max_chars !== project.params.max_chars ||
    params.target_lufs !== project.params.target_lufs ||
    params.silence_gap_ms !== project.params.silence_gap_ms

  const previewMatchesCurrent =
    previewResult !== null &&
    previewResult.num_step === params.num_step &&
    Math.abs(previewResult.guidance_scale - params.guidance_scale) < 0.01 &&
    Math.abs(previewResult.speed - params.speed) < 0.01

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

  const onPreview = async () => {
    setPreviewing(true)
    setMsg('')
    try {
      const result = await previewCustom(slug, {
        num_step: params.num_step,
        guidance_scale: params.guidance_scale,
        speed: params.speed,
      })
      setPreviewResult(result)
      setMsg(result.cached ? 'Cached.' : `Generated in ${result.gen_seconds.toFixed(1)} s.`)
    } catch (e) {
      setMsg(`Preview failed: ${e}`)
    } finally {
      setPreviewing(false)
    }
  }

  const onReset = () => {
    setParams({
      num_step: project.params.num_step,
      guidance_scale: project.params.guidance_scale,
      speed: project.params.speed,
      min_chars: project.params.min_chars,
      max_chars: project.params.max_chars,
      target_lufs: project.params.target_lufs,
      silence_gap_ms: project.params.silence_gap_ms,
    })
    setMsg('')
  }

  return (
    <div className="space-y-5">
      <p className="text-xs text-muted-foreground">
        Direct knobs for OmniVoice. The 4-cell matrix above renders fixed
        presets — these sliders apply to your <em>actual book render</em>.
        After saving, click <strong>Re-generate</strong> on the matrix if
        you want to hear the new params on the sample. Save invalidates
        cached chunks for any chapter not yet rendered with the new params.
      </p>

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
        hint="Stronger conditioning on (text + voice). 2.0 default; 3.0+ may over-emphasize."
        value={params.guidance_scale}
        min={1.0}
        max={4.0}
        step={0.1}
        format={(v) => v.toFixed(1)}
        onChange={(v) => setParams({ ...params, guidance_scale: v })}
      />

      <SliderRow
        label="speed"
        hint="Speech tempo. 1.0 = natural. 0.85 = relaxed, 1.15 = brisk."
        value={params.speed}
        min={0.7}
        max={1.3}
        step={0.05}
        format={(v) => v.toFixed(2) + '×'}
        onChange={(v) => setParams({ ...params, speed: v })}
      />

      <Separator />

      <div className="grid grid-cols-2 gap-4">
        <NumberRow
          label="min_chars"
          hint="Chunk floor (info)"
          value={params.min_chars}
          onChange={(v) => setParams({ ...params, min_chars: v })}
        />
        <NumberRow
          label="max_chars"
          hint="Chunk cap (90–250)"
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
          <Button variant="outline" onClick={onPreview} disabled={previewing}>
            <Wand2 className="h-4 w-4" />
            {previewing ? 'Generating…' : 'Preview these'}
          </Button>
          <Button onClick={onSave} disabled={!dirty || saving}>
            <Save className="h-4 w-4" />
            {saving ? 'Saving…' : 'Save params'}
          </Button>
        </div>
      </div>

      {previewResult && (
        <div className="rounded-md border bg-muted/30 p-3 space-y-2">
          <div className="flex items-center justify-between text-xs">
            <span className="font-medium">
              Custom preview ({previewResult.duration_s.toFixed(1)} s,
              step {previewResult.num_step} · gs{' '}
              {previewResult.guidance_scale.toFixed(1)} · speed{' '}
              {previewResult.speed.toFixed(2)}×)
            </span>
            <span className="text-muted-foreground font-mono">
              {previewMatchesCurrent ? 'matches sliders' : 'sliders changed since render'}
            </span>
          </div>
          <audio
            controls
            src={previewResult.audio_url}
            preload="metadata"
            className="w-full h-9"
          />
        </div>
      )}
    </div>
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
