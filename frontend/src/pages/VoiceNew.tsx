import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Wand2, Save, Loader2, Music, Sparkles, ChevronRight, Play, Pause, Volume2 } from 'lucide-react'
import WaveSurfer from 'wavesurfer.js'
import RegionsPlugin, { type Region } from 'wavesurfer.js/dist/plugins/regions.esm.js'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import {
  analyzeVoiceSource,
  autoTranscribe,
  createVoice,
  draftAudioUrl,
  draftUploadVoice,
  draftUploadVoiceLong,
  extractVoiceWindow,
  getVoiceValidationText,
  listModels,
  previewStagedVoice,
  resetVoiceValidationText,
  setVoiceValidationText,
} from '@/lib/api'
import type {
  AnalyzeResult,
  ChapterMarker,
  DraftUploadLongResult,
  DraftUploadResult,
  StagedVoicePreview,
  TTSModel,
  VoiceCandidate,
} from '@/lib/types'

// Bootstrapping default while the server's saved text is still loading.
// The canonical default lives in audiomat/settings_store.py and is
// fetched via GET /api/settings/voice-validation-text on mount.
const FALLBACK_VALIDATION_TEXT =
  'Bylo už 10 minut po půlnoci, když do dveří hospody vstoupil cizinec ' +
  'v promočeném kabátě. „Dobrý večer," pozdravil tiše. Hostinský zvedl ' +
  'hlavu od novin a změřil si ho pohledem.'

type Stage =
  | 'pick'
  | 'long-uploading'
  | 'chapter-pick'
  | 'analyzing'
  | 'candidates'
  | 'trim'
  | 'review'
  | 'saving'

export function VoiceNew() {
  const nav = useNavigate()
  const fileShortRef = useRef<HTMLInputElement>(null)
  const fileLongRef = useRef<HTMLInputElement>(null)

  const [stage, setStage] = useState<Stage>('pick')

  // short flow: single converted clip
  const [draft, setDraft] = useState<DraftUploadResult | null>(null)

  // long flow state machine
  const [longDraft, setLongDraft] = useState<DraftUploadLongResult | null>(null)
  const [pickedChapter, setPickedChapter] = useState<ChapterMarker | null>(null)
  const [analyzed, setAnalyzed] = useState<AnalyzeResult | null>(null)
  const [pickedCandidate, setPickedCandidate] = useState<VoiceCandidate | null>(null)
  const [trimRange, setTrimRange] = useState<{ start: number; end: number } | null>(null)

  // common metadata
  const [name, setName] = useState('')
  const [transcript, setTranscript] = useState('')
  const [notes, setNotes] = useState('')
  const [ttsModel, setTtsModel] = useState<string>('')
  const [models, setModels] = useState<TTSModel[]>([])
  const [autoBusy, setAutoBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    listModels().then(setModels).catch(() => setModels([]))
  }, [])

  // ------- short flow -------

  const onShortUpload = async (file: File) => {
    setErr('')
    setStage('long-uploading') // reuse spinner stage label (it's just "busy")
    try {
      const d = await draftUploadVoice(file)
      setDraft(d)
      setStage('review')
    } catch (e) {
      setErr(String(e))
      setStage('pick')
    }
  }

  // ------- long flow -------

  const onLongUpload = async (file: File) => {
    setErr('')
    setStage('long-uploading')
    try {
      const d = await draftUploadVoiceLong(file)
      setLongDraft(d)
      // If the source has chapters, let the user pick (or stick with the
      // default = analyze first 10 min). Otherwise jump straight to analyze.
      if (d.chapters.length > 0) {
        setStage('chapter-pick')
      } else {
        await runAnalyze(d, null)
      }
    } catch (e) {
      setErr(String(e))
      setStage('pick')
    }
  }

  const runAnalyze = async (d: DraftUploadLongResult, chapter: ChapterMarker | null) => {
    setStage('analyzing')
    setPickedChapter(chapter)
    try {
      const r = await analyzeVoiceSource({
        audio_path: d.audio_path,
        chapter_start_s: chapter?.start_s ?? null,
        chapter_end_s: chapter?.end_s ?? null,
        analyze_minutes: 10,
      })
      setAnalyzed(r)
      setStage('candidates')
      if (r.candidates.length === 0) {
        setErr('No usable 5–10 s windows found in this slice. Try a different chapter or source.')
      }
    } catch (e) {
      setErr(String(e))
      setStage(d.chapters.length > 0 ? 'chapter-pick' : 'pick')
    }
  }

  const onPickCandidate = (c: VoiceCandidate) => {
    setPickedCandidate(c)
    setTrimRange({ start: 0, end: c.duration_s })
    setStage('trim')
  }

  const onConfirmTrim = async () => {
    if (!analyzed || !pickedCandidate || !trimRange) return
    setErr('')
    try {
      // The trim stage UI lets the user shrink within [0, candidate.duration_s].
      // Convert that back to slice-relative coordinates for the backend.
      const sliceStart = pickedCandidate.start_s + trimRange.start
      const sliceEnd = pickedCandidate.start_s + trimRange.end
      const final = await extractVoiceWindow({
        audio_path: analyzed.full_audio_path,
        analyzed_start_s: analyzed.analyzed_start_s,
        start_s: sliceStart,
        end_s: sliceEnd,
      })
      setDraft(final)
      setStage('review')
    } catch (e) {
      setErr(String(e))
    }
  }

  // ------- review (shared by both flows) -------

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
        tts_model: ttsModel || null,
      })
      nav('/voices')
    } catch (e) {
      setErr(String(e))
      setStage('review')
    }
  }

  // ------------------------------------------------------------------

  return (
    <div className="max-w-3xl space-y-6">
      <div>
        <Button variant="ghost" onClick={() => nav('/voices')} className="-ml-3">
          <ArrowLeft className="h-4 w-4" />
          Back to voices
        </Button>
        <h1 className="text-2xl font-bold mt-2">Add voice</h1>
        <p className="text-sm text-muted-foreground">
          Upload a 5–10 s clip directly, or hand us a longer source (chapter
          mp3, audiobook m4b) and pick a clip from auto-detected candidates.
        </p>
      </div>

      {err && (
        <div className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
          {err}
        </div>
      )}

      {stage === 'pick' && (
        <PickStage
          onShort={() => fileShortRef.current?.click()}
          onLong={() => fileLongRef.current?.click()}
          shortInputRef={fileShortRef}
          longInputRef={fileLongRef}
          onShortFile={onShortUpload}
          onLongFile={onLongUpload}
        />
      )}

      {stage === 'long-uploading' && <Spinner label="Uploading & converting…" />}

      {stage === 'chapter-pick' && longDraft && (
        <ChapterPickStage
          source={longDraft}
          onPick={(c) => runAnalyze(longDraft, c)}
          onUseFirst10Min={() => runAnalyze(longDraft, null)}
        />
      )}

      {stage === 'analyzing' && (
        <Spinner label="Searching for the best 5–10 s reference clips… (~5–20 s)" />
      )}

      {stage === 'candidates' && analyzed && (
        <CandidatesStage
          analyzed={analyzed}
          chapter={pickedChapter}
          onPick={onPickCandidate}
          onBack={() => setStage(longDraft?.chapters.length ? 'chapter-pick' : 'pick')}
        />
      )}

      {stage === 'trim' && analyzed && pickedCandidate && (
        <TrimStage
          candidate={pickedCandidate}
          onChange={setTrimRange}
          onBack={() => setStage('candidates')}
          onConfirm={onConfirmTrim}
        />
      )}

      {(stage === 'review' || stage === 'saving') && draft && (
        <ReviewStage
          draft={draft}
          name={name} setName={setName}
          transcript={transcript} setTranscript={setTranscript}
          notes={notes} setNotes={setNotes}
          ttsModel={ttsModel} setTtsModel={setTtsModel}
          models={models}
          autoBusy={autoBusy}
          onAutoTranscribe={onAutoTranscribe}
          onSave={onSave}
          saving={stage === 'saving'}
          onReupload={() => {
            setDraft(null)
            setLongDraft(null)
            setAnalyzed(null)
            setPickedCandidate(null)
            setStage('pick')
          }}
        />
      )}
    </div>
  )
}

// ----------------------------------------------------------------------------
// Stage components
// ----------------------------------------------------------------------------

function PickStage({
  onShort, onLong, shortInputRef, longInputRef, onShortFile, onLongFile,
}: {
  onShort: () => void
  onLong: () => void
  shortInputRef: React.RefObject<HTMLInputElement | null>
  longInputRef: React.RefObject<HTMLInputElement | null>
  onShortFile: (f: File) => void
  onLongFile: (f: File) => void
}) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <Card className="cursor-pointer hover:border-primary transition-colors" onClick={onShort}>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Music className="h-5 w-5" />
            Short clip (5–10 s)
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Already have a trimmed reference? Upload it directly. We convert to
            24 kHz mono and reject anything longer than 20 s.
          </p>
          <Button onClick={(e) => { e.stopPropagation(); onShort() }}>
            Choose short clip
          </Button>
          <input
            ref={shortInputRef}
            type="file"
            accept="audio/*,.wav,.mp3,.ogg,.flac,.m4a"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) onShortFile(f)
            }}
          />
        </CardContent>
      </Card>

      <Card className="cursor-pointer hover:border-primary transition-colors" onClick={onLong}>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5" />
            Long source (auto-pick)
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Drop a chapter mp3 or audiobook m4b. We'll scan up to 10 minutes,
            find the cleanest 5 phrase-bounded clips, and you pick.
          </p>
          <Button variant="outline" onClick={(e) => { e.stopPropagation(); onLong() }}>
            Choose long source
          </Button>
          <input
            ref={longInputRef}
            type="file"
            accept="audio/*,.wav,.mp3,.m4a,.m4b,.ogg,.flac"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) onLongFile(f)
            }}
          />
        </CardContent>
      </Card>
    </div>
  )
}

function Spinner({ label }: { label: string }) {
  return (
    <Card>
      <CardContent className="py-12 flex items-center justify-center gap-3 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" />
        <span>{label}</span>
      </CardContent>
    </Card>
  )
}

function ChapterPickStage({
  source, onPick, onUseFirst10Min,
}: {
  source: DraftUploadLongResult
  onPick: (c: ChapterMarker) => void
  onUseFirst10Min: () => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Pick what to analyze</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Source has {source.chapters.length} chapters. Pick one to scan, or
          stick with the default (first 10 min of the file).
        </p>
        <Button variant="outline" onClick={onUseFirst10Min}>
          Use first 10 min of file
        </Button>
        <div className="border-t pt-3 -mx-6 px-6">
          <Label className="text-xs uppercase tracking-wide text-muted-foreground">
            Or pick a chapter
          </Label>
          <div className="mt-2 max-h-80 overflow-y-auto divide-y rounded-md border">
            {source.chapters.map((c) => (
              <button
                key={c.index}
                type="button"
                className="w-full text-left px-3 py-2 text-sm hover:bg-secondary/40 flex items-center justify-between gap-3"
                onClick={() => onPick(c)}
              >
                <span className="truncate">{c.title}</span>
                <span className="font-mono text-xs text-muted-foreground shrink-0">
                  {fmtDuration(c.duration_s)}
                </span>
              </button>
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function CandidatesStage({
  analyzed, chapter, onPick, onBack,
}: {
  analyzed: AnalyzeResult
  chapter: ChapterMarker | null
  onPick: (c: VoiceCandidate) => void
  onBack: () => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Pick a candidate clip</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Top {analyzed.candidates.length} clips found in{' '}
          {chapter
            ? <>chapter <strong>{chapter.title}</strong></>
            : <>the first {fmtDuration(analyzed.analyzed_end_s - analyzed.analyzed_start_s)} of the file</>}.
          Listen and pick the one that sounds cleanest.
        </p>
        <div className="space-y-3">
          {analyzed.candidates.map((c) => (
            <CandidateRow key={c.index} c={c} onUse={() => onPick(c)} />
          ))}
        </div>
        <div className="flex justify-start pt-2">
          <Button variant="ghost" onClick={onBack}>
            <ArrowLeft className="h-4 w-4" />
            Back
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function CandidateRow({ c, onUse }: { c: VoiceCandidate; onUse: () => void }) {
  const snrLabel = c.breakdown.snr_db == null ? '—' : `${c.breakdown.snr_db.toFixed(0)} dB`
  return (
    <div className="rounded-md border p-3 space-y-2">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <ScoreBadge score={c.score} />
          <div className="text-sm">
            <div className="font-mono text-xs text-muted-foreground">
              {fmtTimecode(c.start_s)} – {fmtTimecode(c.end_s)} ({c.duration_s.toFixed(1)} s)
            </div>
            <div className="text-xs text-muted-foreground">
              breath {(c.breakdown.density * 100).toFixed(0)}% · SNR {snrLabel} · peak {c.breakdown.peak.toFixed(2)}
            </div>
          </div>
        </div>
        <Button size="sm" onClick={onUse}>
          Use this
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>
      <audio
        controls
        src={draftAudioUrl(c.preview_path)}
        className="w-full h-8"
        preload="none"
      >
        Your browser doesn't support inline audio playback.
      </audio>
    </div>
  )
}

function ScoreBadge({ score }: { score: number }) {
  // 80+ = green, 65-80 = amber, < 65 = red
  const color =
    score >= 80
      ? 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30'
      : score >= 65
      ? 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30'
      : 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30'
  return (
    <div className={`rounded-md border px-2 py-1 text-sm font-mono font-medium ${color}`}>
      {score.toFixed(0)}
    </div>
  )
}

function TrimStage({
  candidate, onChange, onBack, onConfirm,
}: {
  candidate: VoiceCandidate
  onChange: (range: { start: number; end: number }) => void
  onBack: () => void
  onConfirm: () => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const wsRef = useRef<WaveSurfer | null>(null)
  const regionRef = useRef<Region | null>(null)
  const [range, setRange] = useState<{ start: number; end: number }>({
    start: 0,
    end: candidate.duration_s,
  })
  const [playing, setPlaying] = useState(false)
  const [ready, setReady] = useState(false)

  useEffect(() => {
    if (!containerRef.current) return
    setReady(false)
    setPlaying(false)
    const regions = RegionsPlugin.create()
    const ws = WaveSurfer.create({
      container: containerRef.current,
      url: draftAudioUrl(candidate.preview_path),
      waveColor: 'rgb(148 163 184)',
      progressColor: 'rgb(59 130 246)',
      height: 96,
      normalize: true,
      cursorWidth: 1,
      plugins: [regions],
    })
    wsRef.current = ws

    ws.on('ready', () => {
      const r = regions.addRegion({
        start: 0,
        end: candidate.duration_s,
        color: 'rgba(59, 130, 246, 0.18)',
        drag: true,
        resize: true,
      })
      regionRef.current = r
      r.on('update-end', () => {
        const next = { start: r.start, end: r.end }
        setRange(next)
        onChange(next)
      })
      setReady(true)
    })
    ws.on('play', () => setPlaying(true))
    ws.on('pause', () => setPlaying(false))
    ws.on('finish', () => setPlaying(false))

    return () => {
      ws.destroy()
      wsRef.current = null
      regionRef.current = null
    }
    // candidate identity is the dependency — switching candidates rebuilds.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidate.preview_path])

  const dur = range.end - range.start
  const tooShort = dur < 3
  const tooLong = dur > 12

  const togglePlay = () => {
    const ws = wsRef.current
    const r = regionRef.current
    if (!ws || !r) return
    if (ws.isPlaying()) {
      ws.pause()
    } else {
      // region.play() honors the current drag-resized [start, end] —
      // playback always matches the visible selection without us tracking
      // the bounds manually.
      r.play()
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Fine-tune the clip</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Drag the highlighted region's edges to trim. OmniVoice's tested range
          is 5–10 s; we accept 3–12 s with light slack.
        </p>
        <div ref={containerRef} className="rounded-md border bg-secondary/30 p-2" />
        <div className="flex items-center justify-between text-sm gap-3">
          <Button
            variant="outline"
            size="sm"
            onClick={togglePlay}
            disabled={!ready}
            aria-label={playing ? 'Pause selection' : 'Play selection'}
          >
            {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
            {playing ? 'Pause' : 'Play selection'}
          </Button>
          <div className="flex-1 font-mono text-muted-foreground text-right">
            {range.start.toFixed(2)} s – {range.end.toFixed(2)} s
          </div>
          <div className={`font-mono font-medium tabular-nums ${tooShort || tooLong ? 'text-destructive' : ''}`}>
            {dur.toFixed(2)} s
          </div>
        </div>
        <div className="flex justify-between">
          <Button variant="ghost" onClick={onBack}>
            <ArrowLeft className="h-4 w-4" />
            Back
          </Button>
          <Button onClick={onConfirm} disabled={tooShort || tooLong}>
            Confirm clip
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function ReviewStage({
  draft, name, setName, transcript, setTranscript, notes, setNotes,
  ttsModel, setTtsModel, models, autoBusy, onAutoTranscribe, onSave, saving,
  onReupload,
}: {
  draft: DraftUploadResult
  name: string; setName: (v: string) => void
  transcript: string; setTranscript: (v: string) => void
  notes: string; setNotes: (v: string) => void
  ttsModel: string; setTtsModel: (v: string) => void
  models: TTSModel[]
  autoBusy: boolean
  onAutoTranscribe: () => void
  onSave: () => void
  saving: boolean
  onReupload: () => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Review &amp; transcript</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="rounded-md bg-secondary/40 p-3 text-sm space-y-3">
          <div className="grid grid-cols-3 gap-x-4 gap-y-1">
            <span className="text-muted-foreground">Duration</span>
            <span className="font-mono col-span-2 text-right">
              {draft.duration_s.toFixed(2)} s
            </span>
            <span className="text-muted-foreground">Sample rate</span>
            <span className="font-mono col-span-2 text-right">
              {draft.sample_rate} Hz
            </span>
            <span className="text-muted-foreground">Channels</span>
            <span className="font-mono col-span-2 text-right">{draft.channels}</span>
          </div>
          <audio
            controls
            src={draftAudioUrl(draft.audio_path)}
            className="w-full"
            preload="metadata"
          >
            Your browser doesn't support inline audio playback.
          </audio>
          {draft.warning && (
            <p className="text-xs text-amber-600 dark:text-amber-400">
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

        <div className="space-y-2">
          <Label htmlFor="vmodel">TTS model</Label>
          <select
            id="vmodel"
            className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
            value={ttsModel}
            onChange={(e) => setTtsModel(e.target.value)}
          >
            <option value="">OmniVoice (stock — default)</option>
            {models.map((m) => (
              <option key={m.name_slug} value={m.name_slug}>
                {m.name} {m.source_type === 'hf' ? '(HF)' : '(local)'}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">
            Stock OmniVoice handles 600+ languages out of the box. Pick a
            registered fine-tune (manage them under{' '}
            <a href="/models" className="text-primary underline">Models</a>)
            if you've got one targeted at this speaker.
          </p>
        </div>

        <ValidateClipSection
          audioPath={draft.audio_path}
          transcript={transcript}
          ttsModel={ttsModel}
        />

        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onReupload}>
            Re-upload
          </Button>
          <Button
            onClick={onSave}
            disabled={!name.trim() || !transcript.trim() || saving}
          >
            <Save className="h-4 w-4" />
            {saving ? 'Saving…' : 'Save voice'}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

function ValidateClipSection({
  audioPath, transcript, ttsModel,
}: {
  audioPath: string
  transcript: string
  ttsModel: string
}) {
  const [text, setText] = useState(FALLBACK_VALIDATION_TEXT)
  const [isDefault, setIsDefault] = useState(true)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<StagedVoicePreview | null>(null)
  const [err, setErr] = useState('')

  // Load the user's saved validation text (or the server's canonical
  // default if they've never overridden it). Fall back to the bootstrap
  // constant if the settings endpoint is unreachable.
  useEffect(() => {
    getVoiceValidationText()
      .then((s) => {
        setText(s.text)
        setIsDefault(s.is_default)
      })
      .catch(() => {
        // Already initialized to FALLBACK_VALIDATION_TEXT + is_default=true.
      })
  }, [])

  const onRender = async () => {
    if (!transcript.trim()) {
      setErr('Fill in the transcript above first — render needs the matching ref text.')
      return
    }
    setBusy(true)
    setErr('')
    try {
      const r = await previewStagedVoice({
        audio_path: audioPath,
        transcript,
        sample_text: text,
        tts_model_slug: ttsModel || null,
      })
      setResult(r)
      // Persist on Render click — saves what the user actually used,
      // not abandoned drafts. Non-blocking: ignore failures so a flaky
      // settings save doesn't block the much-more-expensive TTS result.
      setVoiceValidationText(text)
        .then((s) => setIsDefault(s.is_default))
        .catch(() => {})
    } catch (e) {
      setErr(String(e))
      setResult(null)
    } finally {
      setBusy(false)
    }
  }

  const onReset = async () => {
    try {
      const s = await resetVoiceValidationText()
      setText(s.text)
      setIsDefault(true)
      setResult(null)
    } catch (e) {
      setErr(String(e))
    }
  }

  // Reset the rendered preview when the user edits the text — stale audio
  // shouldn't sit there labelled as if it matched the new text. Cache on
  // the backend keeps re-renders snappy when they revert.
  useEffect(() => {
    setResult(null)
  }, [text])

  return (
    <div className="space-y-2 pt-2 border-t">
      <div className="flex items-center justify-between gap-3">
        <Label htmlFor="vvalidate" className="text-sm flex items-center gap-2">
          <Volume2 className="h-4 w-4" />
          Validate clone (recommended before save)
        </Label>
        {!isDefault && (
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground underline"
            onClick={onReset}
          >
            Reset to default
          </button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Render this text with the new voice. If it sounds off (slurred,
        wrong accent, robotic), pick a different clip — bad refs only show
        up at TTS time, not on the source picker. Edits are saved on the
        server when you hit Render, so next voice creation starts here.
      </p>
      <Textarea
        id="vvalidate"
        rows={3}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="Sample text to render with this voice…"
      />
      <div className="flex items-center justify-between gap-3">
        <Button
          variant="outline"
          size="sm"
          onClick={onRender}
          disabled={busy || !text.trim()}
        >
          {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : <Volume2 className="h-3 w-3" />}
          {busy ? 'Rendering…' : result ? 'Re-render' : 'Render preview'}
        </Button>
        {result && (
          <span className="text-xs text-muted-foreground font-mono">
            {result.duration_s.toFixed(1)} s
            {result.gen_seconds > 0 && ` · gen ${result.gen_seconds.toFixed(1)} s`}
            {result.gen_seconds === 0 && ' · cached'}
          </span>
        )}
      </div>
      {err && (
        <p className="text-xs text-destructive">{err}</p>
      )}
      {result && (
        <audio
          controls
          src={draftAudioUrl(result.audio_path)}
          className="w-full h-9"
          preload="metadata"
        />
      )}
    </div>
  )
}

// ----------------------------------------------------------------------------
// Helpers
// ----------------------------------------------------------------------------

function fmtDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return m > 0 ? `${m} min ${s.toString().padStart(2, '0')} s` : `${s} s`
}

function fmtTimecode(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}
