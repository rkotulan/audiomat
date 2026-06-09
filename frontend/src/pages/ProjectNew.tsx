import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowLeft, BookOpen, Wand2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { createProject, listModels, listVoices } from '@/lib/api'
import { LANGUAGE_OPTIONS, isValidLanguageCode } from '@/lib/languages'
import { DEFAULT_MODEL_SLUG } from '@/lib/caps'
import type { TTSModel, Voice } from '@/lib/types'

export function ProjectNew() {
  const nav = useNavigate()
  const [searchParams] = useSearchParams()
  const fileRef = useRef<HTMLInputElement>(null)

  const requestedVoiceSlug = searchParams.get('voice') || ''

  const [voices, setVoices] = useState<Voice[]>([])
  const [models, setModels] = useState<TTSModel[]>([])
  const [name, setName] = useState('')
  const [voiceRef, setVoiceRef] = useState('')
  // v0.5: explicit engine pick at create time. The dropdown auto-
  // suggests whatever the picked voice was tested with (so the common
  // case — clone a Higgs voice then create a project with it — needs
  // no extra clicks). User can override.
  const [ttsModel, setTtsModel] = useState<string>(DEFAULT_MODEL_SLUG)
  // Track whether the user has manually changed the engine pick — once
  // they do, voice swaps stop re-overriding their choice. Otherwise
  // switching voices would silently flip the engine they just set.
  const [engineUserPicked, setEngineUserPicked] = useState(false)
  const [book, setBook] = useState<File | null>(null)
  const [language, setLanguage] = useState('cs')
  const [customLanguage, setCustomLanguage] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [dragActive, setDragActive] = useState(false)

  useEffect(() => {
    Promise.all([listVoices(), listModels().catch(() => [])])
      .then(([vs, ms]) => {
        setVoices(vs)
        setModels(ms)
        if (voiceRef) return
        const fromQuery = vs.find((v) => v.name_slug === requestedVoiceSlug)
        if (fromQuery) {
          setVoiceRef(fromQuery.name)
        } else if (vs.length > 0) {
          setVoiceRef(vs[0].name)
        }
      })
      .catch((e) => setErr(String(e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Auto-suggest engine from the picked voice's "tested with" model.
  // The voice library uses that field informationally in v0.5, but on
  // a fresh project it's the best hint we have — clone a Higgs voice
  // then pick it for a project = obviously the project should use
  // Higgs too. User can still flip via the Engine dropdown.
  useEffect(() => {
    if (engineUserPicked) return
    const v = voices.find((x) => x.name === voiceRef)
    setTtsModel(v?.tts_model || DEFAULT_MODEL_SLUG)
  }, [voiceRef, voices, engineUserPicked])

  const isTxt = book?.name.toLowerCase().endsWith('.txt') ?? false
  const effectiveLanguage =
    language === 'other' ? customLanguage.trim().toLowerCase() : language
  const txtLangValid = !isTxt || isValidLanguageCode(effectiveLanguage)

  const canSubmit =
    name.trim() && voiceRef.trim() && book && txtLangValid && !busy

  const acceptFile = (f: File | null) => {
    if (!f) return
    const lower = f.name.toLowerCase()
    if (!lower.endsWith('.epub') && !lower.endsWith('.txt')) {
      setErr(`Only EPUB or TXT files supported (got ${f.name})`)
      return
    }
    setBook(f)
    setErr('')
  }

  const onDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    setDragActive(false)
    const f = e.dataTransfer.files?.[0]
    if (f) acceptFile(f)
  }

  const onSubmit = async () => {
    if (!book) return
    setBusy(true)
    setErr('')
    try {
      const p = await createProject({
        name: name.trim(),
        voice_ref: voiceRef,
        book,
        // Backend ignores `language` for EPUB unless DC metadata is
        // missing, so it's safe to always send.
        language: isTxt ? effectiveLanguage : undefined,
        tts_model: ttsModel === DEFAULT_MODEL_SLUG ? null : ttsModel,
      })
      nav(`/projects/${p.name_slug}`)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <Button variant="ghost" onClick={() => nav('/projects')} className="-ml-3">
          <ArrowLeft className="h-4 w-4" />
          Back to projects
        </Button>
        <h1 className="text-2xl font-bold mt-2">New project</h1>
        <p className="text-sm text-muted-foreground">
          One project = one book + one voice. The name is permanent.
        </p>
      </div>

      {err && (
        <div className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
          {err}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Project</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="pname">Project name</Label>
            <Input
              id="pname"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Skleněný muž"
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="pvoice">Voice</Label>
            {voices.length === 0 ? (
              <div className="text-sm text-muted-foreground rounded-md border p-3">
                No voices in library yet.{' '}
                <Link to="/voices/new" className="text-primary underline">
                  Add one first
                </Link>
                .
              </div>
            ) : (
              <select
                id="pvoice"
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
                value={voiceRef}
                onChange={(e) => setVoiceRef(e.target.value)}
              >
                {voices.map((v) => (
                  <option key={v.name_slug} value={v.name}>
                    {v.name} ({v.duration_s.toFixed(1)} s)
                  </option>
                ))}
              </select>
            )}
          </div>

          {/* v0.5: TTS engine pick. Defaults to whatever the selected
              voice was tested with (auto-suggest), or stock OmniVoice
              when the voice has no model assigned. User can override
              before submit. */}
          <div className="space-y-2">
            <Label htmlFor="ptts">TTS engine</Label>
            <select
              id="ptts"
              className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
              value={ttsModel}
              onChange={(e) => {
                setTtsModel(e.target.value)
                setEngineUserPicked(true)
              }}
              disabled={models.length === 0}
            >
              {models.length === 0 && <option value={DEFAULT_MODEL_SLUG}>Loading…</option>}
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
            {(() => {
              const sel = models.find((m) => m.name_slug === ttsModel)
              if (!sel) return null
              if (sel.license === 'non_commercial') {
                return (
                  <p className="text-xs text-amber-700 dark:text-amber-300">
                    Non-commercial license: {sel.capabilities.license_name}.
                    Renders made with this engine carry its obligations.
                  </p>
                )
              }
              return (
                <p className="text-xs text-muted-foreground">
                  You can switch engines later on the project's Advanced tab.
                </p>
              )
            })()}
          </div>

          <div className="space-y-2">
            <Label>Book file</Label>
            <input
              ref={fileRef}
              type="file"
              accept=".epub,.txt"
              className="hidden"
              onChange={(e) => acceptFile(e.target.files?.[0] || null)}
            />
            <div
              onClick={() => fileRef.current?.click()}
              onDragEnter={(e) => {
                e.preventDefault()
                setDragActive(true)
              }}
              onDragOver={(e) => {
                e.preventDefault()
                setDragActive(true)
              }}
              onDragLeave={(e) => {
                // Only flip false when leaving the zone, not when crossing
                // a child element boundary.
                if (e.currentTarget.contains(e.relatedTarget as Node)) return
                setDragActive(false)
              }}
              onDrop={onDrop}
              className={[
                'rounded-md border-2 border-dashed px-6 py-8 text-center cursor-pointer transition-colors select-none',
                dragActive
                  ? 'border-primary bg-primary/5'
                  : book
                  ? 'border-muted-foreground/40 bg-muted/30'
                  : 'border-muted-foreground/25 hover:bg-muted/30',
              ].join(' ')}
            >
              <BookOpen className="h-8 w-8 mx-auto mb-2 text-muted-foreground" />
              {book ? (
                <>
                  <p className="text-sm font-medium">{book.name}</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    {(book.size / 1024).toFixed(0)} KB · click or drop to replace
                  </p>
                </>
              ) : (
                <>
                  <p className="text-sm font-medium">
                    {dragActive ? 'Drop to upload' : 'Drop EPUB or TXT here, or click to browse'}
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">
                    EPUB metadata is auto-detected. TXT needs a language pick.
                  </p>
                </>
              )}
            </div>
          </div>

          {isTxt && (
            <div className="space-y-2">
              <Label htmlFor="plang">Language</Label>
              <select
                id="plang"
                className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              >
                {LANGUAGE_OPTIONS.map((opt) => (
                  <option key={opt.code} value={opt.code}>
                    {opt.label}
                  </option>
                ))}
                <option value="other">Other (custom code)…</option>
              </select>
              {language === 'other' && (
                <Input
                  value={customLanguage}
                  onChange={(e) => setCustomLanguage(e.target.value)}
                  placeholder="ISO 639-1 / BCP 47 (e.g. ja, ko, pt-BR)"
                />
              )}
              <p className="text-xs text-muted-foreground">
                TXT files don't include language metadata. Pick the language so
                numbers get spelled out correctly ("1959" → "tisíc devět set
                padesát devět" for cs).
              </p>
              {!txtLangValid && (
                <p className="text-xs text-destructive">
                  Invalid language code — use ISO 639-1 (e.g. cs, en) or BCP 47
                  (e.g. pt-BR).
                </p>
              )}
            </div>
          )}

          <div className="flex justify-end">
            <Button onClick={onSubmit} disabled={!canSubmit}>
              <Wand2 className="h-4 w-4" />
              {busy ? 'Creating…' : 'Create project'}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
