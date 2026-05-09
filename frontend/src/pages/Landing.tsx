import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { BookOpen, Library, Plus, AudioLines } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { listProjects, listVoices } from '@/lib/api'
import type { Project, Voice } from '@/lib/types'

export function Landing() {
  const [projects, setProjects] = useState<Project[] | null>(null)
  const [voices, setVoices] = useState<Voice[] | null>(null)
  const [error, setError] = useState<string>('')

  useEffect(() => {
    Promise.all([listProjects(), listVoices()])
      .then(([p, v]) => {
        setProjects(p)
        setVoices(v)
      })
      .catch((e) => setError(String(e)))
  }, [])

  return (
    <div className="space-y-10">
      <section className="text-center py-8">
        <h1 className="text-4xl font-bold tracking-tight">audiomat</h1>
        <p className="mt-2 text-muted-foreground">
          Convert eBooks into audiobooks with cloned voices, locally and offline.
        </p>
      </section>

      <section className="grid md:grid-cols-2 gap-6">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <BookOpen className="h-5 w-5" />
              Projects
            </CardTitle>
            <CardDescription>
              {projects === null
                ? 'Loading…'
                : projects.length === 0
                  ? 'No projects yet — create one to get started.'
                  : `${projects.length} project${projects.length === 1 ? '' : 's'} in your library.`}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {projects?.slice(0, 5).map((p) => (
              <ProjectCardRow key={p.name_slug} project={p} />
            ))}
            <div className="flex gap-2">
              <Button asChild>
                <Link to="/projects/new">
                  <Plus className="h-4 w-4" />
                  New project
                </Link>
              </Button>
              {projects && projects.length > 0 && (
                <Button variant="outline" asChild>
                  <Link to="/projects">All projects</Link>
                </Button>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Library className="h-5 w-5" />
              Voices
            </CardTitle>
            <CardDescription>
              {voices === null
                ? 'Loading…'
                : voices.length === 0
                  ? 'No voices yet — upload a 5–10 s reference.'
                  : `${voices.length} voice${voices.length === 1 ? '' : 's'} in your library.`}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {voices?.slice(0, 5).map((v) => (
              <div key={v.name_slug} className="flex items-center justify-between text-sm">
                <span className="flex items-center gap-2">
                  <AudioLines className="h-4 w-4 text-muted-foreground" />
                  {v.name}
                </span>
                <span className="text-xs text-muted-foreground">
                  {v.duration_s.toFixed(1)} s · {v.sample_rate} Hz
                </span>
              </div>
            ))}
            <div className="flex gap-2">
              <Button asChild>
                <Link to="/voices/new">
                  <Plus className="h-4 w-4" />
                  Add voice
                </Link>
              </Button>
              {voices && voices.length > 0 && (
                <Button variant="outline" asChild>
                  <Link to="/voices">All voices</Link>
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      </section>

      {error && (
        <div className="rounded-md border border-destructive bg-destructive/10 p-4 text-sm">
          <p className="font-medium text-destructive">Couldn't reach the API</p>
          <p className="text-muted-foreground mt-1">{error}</p>
          <p className="text-muted-foreground mt-2 text-xs">
            Run <code>uvicorn audiomat.api:app --reload --port 8000</code> in another terminal.
          </p>
        </div>
      )}
    </div>
  )
}

function ProjectCardRow({ project }: { project: Project }) {
  const { status } = project
  const pct = status.chapters_total
    ? Math.round((status.chapters_done / status.chapters_total) * 100)
    : 0
  return (
    <Link
      to={`/projects/${project.name_slug}`}
      className="block rounded-md border p-3 hover:bg-secondary/40 transition-colors"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium truncate">{project.name}</span>
        <Badge variant={phaseVariant(status.phase)}>{status.phase}</Badge>
      </div>
      <div className="mt-1 text-xs text-muted-foreground flex justify-between">
        <span>
          voice: <span className="font-mono">{project.voice_ref}</span>
        </span>
        <span>
          {status.chapters_done}/{status.chapters_total} ({pct}%)
        </span>
      </div>
    </Link>
  )
}

function phaseVariant(
  phase: Project['status']['phase'],
): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (phase === 'complete') return 'default'
  if (phase === 'failed') return 'destructive'
  if (phase === 'rendering') return 'secondary'
  return 'outline'
}
