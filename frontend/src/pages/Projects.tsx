import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Plus, BookOpen } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { listProjects } from '@/lib/api'
import type { Project } from '@/lib/types'

export function Projects() {
  const [projects, setProjects] = useState<Project[] | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    listProjects()
      .then(setProjects)
      .catch((e) => setErr(String(e)))
  }, [])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Projects</h1>
          <p className="text-sm text-muted-foreground">
            One audiobook per project. Cache + manifest are per-chapter.
          </p>
        </div>
        <Button asChild>
          <Link to="/projects/new">
            <Plus className="h-4 w-4" />
            New project
          </Link>
        </Button>
      </div>

      {err && <div className="text-sm text-destructive">{err}</div>}

      {projects === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : projects.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12 text-muted-foreground">
            No projects yet.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-3">
          {projects.map((p) => (
            <Link
              key={p.name_slug}
              to={`/projects/${p.name_slug}`}
              className="block rounded-md border p-4 hover:bg-secondary/40 transition-colors"
            >
              <div className="flex items-center justify-between gap-3">
                <span className="font-medium flex items-center gap-2">
                  <BookOpen className="h-4 w-4 text-muted-foreground" />
                  {p.name}
                </span>
                <Badge variant={phaseVariant(p.status.phase)}>{p.status.phase}</Badge>
              </div>
              <div className="mt-2 grid grid-cols-3 gap-2 text-xs text-muted-foreground">
                <span>
                  voice: <span className="font-mono">{p.voice_ref}</span>
                </span>
                <span>
                  {p.status.chapters_done}/{p.status.chapters_total} chapters
                </span>
                <span className="text-right">
                  step {p.params.num_step} · gs {p.params.guidance_scale.toFixed(1)}
                </span>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
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
