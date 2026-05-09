import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { Plus, Trash2, Play } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { listVoices, deleteVoice, voiceAudioUrl } from '@/lib/api'
import type { Voice } from '@/lib/types'

export function Voices() {
  const [voices, setVoices] = useState<Voice[] | null>(null)
  const [error, setError] = useState('')

  const refresh = () =>
    listVoices()
      .then(setVoices)
      .catch((e) => setError(String(e)))

  useEffect(() => {
    refresh()
  }, [])

  const onDelete = async (slug: string, name: string) => {
    if (!confirm(`Delete voice "${name}"? This is permanent.`)) return
    try {
      await deleteVoice(slug)
      refresh()
    } catch (e) {
      alert(String(e))
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Voices</h1>
          <p className="text-sm text-muted-foreground">
            Voice references re-usable across projects.
          </p>
        </div>
        <Button asChild>
          <Link to="/voices/new">
            <Plus className="h-4 w-4" />
            Add voice
          </Link>
        </Button>
      </div>

      {error && <div className="text-sm text-destructive">{error}</div>}

      {voices === null ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : voices.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12 text-muted-foreground">
            No voices yet. Upload a 5–10 s WAV with matching transcript.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {voices.map((v) => (
            <Card key={v.name_slug}>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center justify-between">
                  <span>{v.name}</span>
                  <Badge variant="outline" className="font-normal">
                    {v.duration_s.toFixed(1)} s
                  </Badge>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
                  <span>Sample rate</span>
                  <span className="font-mono text-right">{v.sample_rate} Hz</span>
                  <span>Channels</span>
                  <span className="font-mono text-right">{v.channels}</span>
                  <span>Transcript</span>
                  <span className="font-mono text-right">{v.transcript_chars} chars</span>
                  <span>Created</span>
                  <span className="font-mono text-right">{formatDate(v.created)}</span>
                </div>
                {v.notes && <p className="text-xs italic">{v.notes}</p>}
                <div className="flex gap-2">
                  <Button size="sm" variant="outline" asChild>
                    <a
                      href={voiceAudioUrl(v.name_slug)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <Play className="h-3 w-3" />
                      Preview
                    </a>
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive hover:text-destructive"
                    onClick={() => onDelete(v.name_slug, v.name)}
                  >
                    <Trash2 className="h-3 w-3" />
                    Delete
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

function formatDate(iso: string) {
  if (!iso) return '—'
  try {
    return new Date(iso).toISOString().slice(0, 10)
  } catch {
    return iso
  }
}
