import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, BookOpen, Cpu, Plus, Trash2, Download } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import {
  deleteVoice,
  listModels,
  listVoices,
  updateVoiceModel,
  voiceAudioUrl,
} from '@/lib/api'
import type { TTSModel, Voice } from '@/lib/types'
import { useConfirm } from '@/components/ConfirmDialog'

export function Voices() {
  const [voices, setVoices] = useState<Voice[] | null>(null)
  const [models, setModels] = useState<TTSModel[]>([])
  const [error, setError] = useState('')
  const [savingModelFor, setSavingModelFor] = useState<string | null>(null)
  const { confirm, dialog: confirmDialog } = useConfirm()

  const refresh = () =>
    listVoices()
      .then(setVoices)
      .catch((e) => setError(String(e)))

  useEffect(() => {
    refresh()
    // Models page is the canonical source for registered TTS models;
    // if it's empty / fails we still render the picker as just "stock".
    listModels()
      .then(setModels)
      .catch(() => setModels([]))
  }, [])

  const onChangeModel = async (slug: string, newModel: string) => {
    setSavingModelFor(slug)
    setError('')
    try {
      const updated = await updateVoiceModel(slug, newModel || null)
      // Patch the voice in place so the select reflects the saved value
      // without a full refetch round-trip.
      setVoices((prev) =>
        prev ? prev.map((v) => (v.name_slug === slug ? updated : v)) : prev,
      )
    } catch (e) {
      setError(String(e))
    } finally {
      setSavingModelFor(null)
    }
  }

  const onDelete = (slug: string, name: string) => {
    confirm({
      title: `Delete voice "${name}"?`,
      description:
        'This permanently removes the voice WAV + transcript + meta. Projects that reference this voice will fail to render until you pick another one.',
      confirmText: 'Delete voice',
      destructive: true,
      onConfirm: async () => {
        try {
          await deleteVoice(slug)
          refresh()
        } catch (e) {
          setError(String(e))
        }
      },
    })
  }

  return (
    <div className="space-y-6">
      {confirmDialog}
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
        <Card className="bg-secondary/40 border-dashed">
          <CardContent className="py-4 flex items-center justify-between gap-4">
            <div className="text-sm">
              <p className="font-medium">Next: create a project</p>
              <p className="text-muted-foreground">
                Pick a voice + an EPUB to render an audiobook.
              </p>
            </div>
            <Button asChild>
              <Link to="/projects/new">
                <BookOpen className="h-4 w-4" />
                New project
                <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          </CardContent>
        </Card>
      )}

      {voices && voices.length > 0 && (
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

                <div className="space-y-1.5 pt-1">
                  <Label
                    htmlFor={`vmodel-${v.name_slug}`}
                    className="text-xs flex items-center gap-1.5"
                  >
                    <Cpu className="h-3 w-3" />
                    TTS model
                    {savingModelFor === v.name_slug && (
                      <span className="ml-auto text-xs text-muted-foreground italic">
                        saving…
                      </span>
                    )}
                  </Label>
                  <select
                    id={`vmodel-${v.name_slug}`}
                    className="flex h-8 w-full rounded-md border border-input bg-transparent px-2 py-1 text-xs shadow-sm"
                    value={v.tts_model ?? ''}
                    onChange={(e) => onChangeModel(v.name_slug, e.target.value)}
                    disabled={savingModelFor === v.name_slug}
                  >
                    <option value="">OmniVoice (stock)</option>
                    {models.map((m) => (
                      <option key={m.name_slug} value={m.name_slug}>
                        {m.name} ({m.source_type === 'hf' ? 'HF' : 'local'})
                      </option>
                    ))}
                  </select>
                </div>

                <audio
                  controls
                  src={voiceAudioUrl(v.name_slug)}
                  preload="metadata"
                  className="w-full h-9"
                />
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" asChild>
                    <Link to={`/projects/new?voice=${v.name_slug}`}>
                      <BookOpen className="h-3 w-3" />
                      Use in project
                    </Link>
                  </Button>
                  <Button size="sm" variant="outline" asChild>
                    <a
                      href={voiceAudioUrl(v.name_slug)}
                      download={`${v.name_slug}.wav`}
                    >
                      <Download className="h-3 w-3" />
                      Download
                    </a>
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-destructive hover:text-destructive ml-auto"
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
