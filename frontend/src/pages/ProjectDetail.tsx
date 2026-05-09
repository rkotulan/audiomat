import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import { ArrowLeft, Play, Hammer, Download, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Progress } from '@/components/ui/progress'
import { Badge } from '@/components/ui/badge'
import { Separator } from '@/components/ui/separator'
import {
  buildM4b,
  deleteProject,
  getProject,
  projectM4bUrl,
  startRender,
  subscribeProgress,
} from '@/lib/api'
import type { Project, ProgressEvent } from '@/lib/types'

export function ProjectDetail() {
  const { slug = '' } = useParams()
  const nav = useNavigate()

  const [project, setProject] = useState<Project | null>(null)
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

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
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

          <Card>
            <CardHeader>
              <CardTitle>Render params</CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <Row label="num_step" value={String(project.params.num_step)} mono />
              <Row
                label="guidance_scale"
                value={project.params.guidance_scale.toFixed(2)}
                mono
              />
              <Row label="speed" value={project.params.speed.toFixed(2)} mono />
              <Row
                label="chunks (chars)"
                value={`${project.params.min_chars}–${project.params.max_chars}`}
                mono
              />
              <Row
                label="target LUFS"
                value={project.params.target_lufs.toFixed(1)}
                mono
              />
              <Row
                label="silence gap"
                value={`${project.params.silence_gap_ms} ms`}
                mono
              />
            </CardContent>
          </Card>
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
              <CardTitle>Danger zone</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <p className="text-sm text-muted-foreground">
                Param tuning UI lands in v0.0.1. For now edit{' '}
                <code className="text-xs">config.json</code> in the project directory
                directly, or use the API:
              </p>
              <code className="block text-xs bg-muted p-2 rounded">
                PATCH /api/projects/{slug}/params
              </code>
              <Separator className="my-4" />
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
