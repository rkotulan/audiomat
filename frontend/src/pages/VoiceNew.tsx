import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Wand2, Save } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import {
  autoTranscribe,
  createVoice,
  draftUploadVoice,
} from '@/lib/api'
import type { DraftUploadResult } from '@/lib/types'

export function VoiceNew() {
  const nav = useNavigate()
  const fileRef = useRef<HTMLInputElement>(null)

  const [stage, setStage] = useState<'pick' | 'review' | 'saving'>('pick')
  const [draft, setDraft] = useState<DraftUploadResult | null>(null)
  const [name, setName] = useState('')
  const [transcript, setTranscript] = useState('')
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [autoBusy, setAutoBusy] = useState(false)

  const onUpload = async (file: File) => {
    setErr('')
    setBusy(true)
    try {
      const d = await draftUploadVoice(file)
      setDraft(d)
      setStage('review')
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const onAutoTranscribe = async () => {
    if (!draft) return
    setAutoBusy(true)
    setErr('')
    try {
      const r = await autoTranscribe(draft.audio_path)
      setTranscript(r.transcript)
    } catch (e) {
      setErr(String(e))
    } finally {
      setAutoBusy(false)
    }
  }

  const onSave = async () => {
    if (!draft || !name.trim() || !transcript.trim()) return
    setStage('saving')
    setErr('')
    try {
      await createVoice({
        name: name.trim(),
        audio_path: draft.audio_path,
        transcript,
        notes,
      })
      nav('/voices')
    } catch (e) {
      setErr(String(e))
      setStage('review')
    }
  }

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <Button variant="ghost" onClick={() => nav('/voices')} className="-ml-3">
          <ArrowLeft className="h-4 w-4" />
          Back to voices
        </Button>
        <h1 className="text-2xl font-bold mt-2">Add voice</h1>
        <p className="text-sm text-muted-foreground">
          Upload a 5–10 s WAV (any format — we convert to 24 kHz mono).
          Then provide a matching transcript.
        </p>
      </div>

      {err && (
        <div className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
          {err}
        </div>
      )}

      {stage === 'pick' && (
        <Card>
          <CardHeader>
            <CardTitle>1. Upload audio</CardTitle>
          </CardHeader>
          <CardContent>
            <input
              ref={fileRef}
              type="file"
              accept="audio/*,.wav,.mp3,.ogg,.flac,.m4a"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) onUpload(f)
              }}
            />
            <Button onClick={() => fileRef.current?.click()} disabled={busy}>
              {busy ? 'Converting…' : 'Choose audio file'}
            </Button>
            <p className="mt-3 text-xs text-muted-foreground">
              OmniVoice's tested range is 3–10 s. We'll reject anything &gt; 20 s
              and warn between 15–20 s.
            </p>
          </CardContent>
        </Card>
      )}

      {(stage === 'review' || stage === 'saving') && draft && (
        <Card>
          <CardHeader>
            <CardTitle>2. Review &amp; transcript</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="rounded-md bg-secondary/40 p-3 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">Duration</span>
                <span className="font-mono">{draft.duration_s.toFixed(2)} s</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Sample rate</span>
                <span className="font-mono">{draft.sample_rate} Hz</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Channels</span>
                <span className="font-mono">{draft.channels}</span>
              </div>
              {draft.warning && (
                <p className="mt-2 text-xs text-amber-600 dark:text-amber-400">
                  ⚠ {draft.warning}
                </p>
              )}
            </div>

            <div className="space-y-2">
              <Label htmlFor="vname">Voice name</Label>
              <Input
                id="vname"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Lucie Ježková"
              />
            </div>

            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <Label htmlFor="vtxt">Transcript</Label>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onAutoTranscribe}
                  disabled={autoBusy}
                >
                  <Wand2 className="h-3 w-3" />
                  {autoBusy ? 'Transcribing…' : 'Auto-draft (Whisper)'}
                </Button>
              </div>
              <Textarea
                id="vtxt"
                rows={6}
                value={transcript}
                onChange={(e) => setTranscript(e.target.value)}
                placeholder="Exact transcript of what's said in the audio…"
              />
              <p className="text-xs text-muted-foreground">
                The transcript MUST match the audio content. Mismatches break
                OmniVoice's tempo estimation (output ends up sped-up gibberish).
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="vnotes">Notes (optional)</Label>
              <Input
                id="vnotes"
                value={notes}
                onChange={(e) => setNotes(e.target.value)}
                placeholder="e.g. clean studio recording, neutral mood"
              />
            </div>

            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setStage('pick')}>
                Re-upload
              </Button>
              <Button
                onClick={onSave}
                disabled={!name.trim() || !transcript.trim() || stage === 'saving'}
              >
                <Save className="h-4 w-4" />
                {stage === 'saving' ? 'Saving…' : 'Save voice'}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
