import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ArrowRight, BookOpen, ChevronDown, ChevronUp, Cpu, Download, Plus, Trash2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Label } from '@/components/ui/label'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
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

  // When a delete is rejected with 409, we stash the in-use info here
  // so the replacement-picker dialog can open. null = dialog closed.
  const [replaceCtx, setReplaceCtx] = useState<{
    slug: string
    name: string
    referencingProjects: { slug: string; name: string }[]
  } | null>(null)
  const [replacementSlug, setReplacementSlug] = useState<string>('')
  const [replaceBusy, setReplaceBusy] = useState(false)

  const runDelete = async (slug: string, replacement?: string) => {
    try {
      await deleteVoice(slug, replacement)
      setReplaceCtx(null)
      refresh()
    } catch (e: unknown) {
      // Typed 409 → open replacement-picker dialog. Anything else → surface.
      if (
        e && typeof e === 'object' &&
        (e as { status?: number }).status === 409 &&
        'referencing_projects' in (e as object)
      ) {
        const inUse = e as {
          referencing_projects: { slug: string; name: string }[]
        }
        const v = voices?.find((x) => x.name_slug === slug)
        setReplaceCtx({
          slug,
          name: v?.name ?? slug,
          referencingProjects: inUse.referencing_projects,
        })
        // Default the dropdown to the first other voice in the library.
        const firstOther = voices?.find((x) => x.name_slug !== slug)
        setReplacementSlug(firstOther?.name_slug ?? '')
      } else {
        setError(String(e))
      }
    }
  }

  const onDelete = (slug: string, name: string) => {
    confirm({
      title: `Delete voice "${name}"?`,
      description:
        'This permanently removes the voice WAV + transcript + meta. If projects reference this voice, you\'ll be asked to pick a replacement before delete.',
      confirmText: 'Delete voice',
      destructive: true,
      onConfirm: () => runDelete(slug),
    })
  }

  const onConfirmReplaceAndDelete = async () => {
    if (!replaceCtx || !replacementSlug) return
    setReplaceBusy(true)
    setError('')
    try {
      await runDelete(replaceCtx.slug, replacementSlug)
    } finally {
      setReplaceBusy(false)
    }
  }

  const otherVoices = voices?.filter((v) => v.name_slug !== replaceCtx?.slug) ?? []

  return (
    <div className="space-y-6">
      {confirmDialog}

      <Dialog
        open={replaceCtx !== null}
        onOpenChange={(open) => {
          if (!open) setReplaceCtx(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Replace and delete &quot;{replaceCtx?.name}&quot;
            </DialogTitle>
            <DialogDescription>
              This voice is referenced by {replaceCtx?.referencingProjects.length}{' '}
              project(s). Pick another voice and we'll reassign those projects
              before deletion. Rendered chapters stay cached but will re-render
              on next run (the manifest signature embeds the voice slug).
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="rounded-md border bg-muted/40 p-2 text-xs space-y-1">
              <p className="font-medium text-muted-foreground uppercase tracking-wide">
                Projects to reassign
              </p>
              <ul className="font-mono space-y-0.5">
                {replaceCtx?.referencingProjects.map((p) => (
                  <li key={p.slug}>{p.name}</li>
                ))}
              </ul>
            </div>
            <div className="space-y-2">
              <Label htmlFor="vreplace">Replace with</Label>
              {otherVoices.length === 0 ? (
                <p className="text-sm text-destructive">
                  No other voices in library — add one first or detach the
                  projects manually before deleting.
                </p>
              ) : (
                <select
                  id="vreplace"
                  className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
                  value={replacementSlug}
                  onChange={(e) => setReplacementSlug(e.target.value)}
                >
                  {otherVoices.map((v) => (
                    <option key={v.name_slug} value={v.name_slug}>
                      {v.name} ({v.duration_s.toFixed(1)} s)
                    </option>
                  ))}
                </select>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setReplaceCtx(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={onConfirmReplaceAndDelete}
              disabled={replaceBusy || !replacementSlug || otherVoices.length === 0}
            >
              <Trash2 className="h-4 w-4" />
              {replaceBusy ? 'Replacing…' : 'Replace & delete'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
            <VoiceCard
              key={v.name_slug}
              voice={v}
              models={models}
              saving={savingModelFor === v.name_slug}
              onChangeModel={(slug) => onChangeModel(v.name_slug, slug)}
              onDelete={() => onDelete(v.name_slug, v.name)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function VoiceCard({
  voice: v, models, saving, onChangeModel, onDelete,
}: {
  voice: Voice
  models: TTSModel[]
  saving: boolean
  onChangeModel: (slug: string) => void
  onDelete: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between">
          <span>{v.name}</span>
          <Badge variant="outline" className="font-normal">
            {v.duration_s.toFixed(1)} s
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <audio
          controls
          src={voiceAudioUrl(v.name_slug)}
          preload="metadata"
          className="w-full h-9"
        />
        <div className="flex flex-wrap items-center gap-2">
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
            className="text-muted-foreground ml-auto"
            onClick={() => setOpen((o) => !o)}
            aria-expanded={open}
          >
            {open ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
            {open ? 'Hide details' : 'Details'}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="text-destructive hover:text-destructive"
            onClick={onDelete}
          >
            <Trash2 className="h-3 w-3" />
            Delete
          </Button>
        </div>

        {open && (
          <div className="space-y-3 pt-2 border-t">
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
            <div className="space-y-1.5">
              <Label
                htmlFor={`vmodel-${v.name_slug}`}
                className="text-xs flex items-center gap-1.5"
              >
                <Cpu className="h-3 w-3" />
                Tested with
                <span
                  className="text-muted-foreground"
                  title="The engine used when validating this voice in the clone preview. Projects pick their own engine on the Advanced tab — this setting only drives the voice picker preview, not the project render."
                >
                  (?)
                </span>
                {saving && (
                  <span className="ml-auto text-xs text-muted-foreground italic">
                    saving…
                  </span>
                )}
              </Label>
              <select
                id={`vmodel-${v.name_slug}`}
                className="flex h-8 w-full rounded-md border border-input bg-transparent px-2 py-1 text-xs shadow-sm"
                value={v.tts_model ?? ''}
                onChange={(e) => onChangeModel(e.target.value)}
                disabled={saving}
              >
                <option value="">OmniVoice (stock)</option>
                {models.map((m) => (
                  <option key={m.name_slug} value={m.name_slug}>
                    {m.name} · {m.capabilities.short_label}
                    {m.license === 'non_commercial' ? ' · non-commercial' : ''}
                    {' '}({m.source_type === 'hf' ? 'HF' : 'local'})
                  </option>
                ))}
              </select>
              {(() => {
                const selected = models.find(
                  (m) => m.name_slug === (v.tts_model ?? ''),
                )
                if (!selected || selected.license !== 'non_commercial') return null
                return (
                  <p className="text-xs text-amber-600 dark:text-amber-400 flex items-start gap-1 pt-1">
                    <span className="shrink-0">⚠</span>
                    <span>
                      <strong>{selected.name}</strong>'s weights ship under a
                      non-commercial license. Audiomat code stays MIT.
                    </span>
                  </p>
                )
              })()}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
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
