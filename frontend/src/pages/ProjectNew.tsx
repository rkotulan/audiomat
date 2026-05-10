import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowLeft, BookOpen, Wand2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { createProject, listVoices } from '@/lib/api'
import type { Voice } from '@/lib/types'

// Common audiobook languages. ISO 639-1 codes map directly to OmniVoice's
// supported set (646 languages — but listing them all in a dropdown is
// noise). Pick the top 10 + Other so most users hit it first try.
const LANGUAGE_OPTIONS: Array<{ code: string; label: string }> = [
  { code: 'cs', label: 'Čeština (cs)' },
  { code: 'sk', label: 'Slovenčina (sk)' },
  { code: 'en', label: 'English (en)' },
  { code: 'de', label: 'Deutsch (de)' },
  { code: 'pl', label: 'Polski (pl)' },
  { code: 'fr', label: 'Français (fr)' },
  { code: 'es', label: 'Español (es)' },
  { code: 'it', label: 'Italiano (it)' },
  { code: 'ru', label: 'Русский (ru)' },
  { code: 'uk', label: 'Українська (uk)' },
]

export function ProjectNew() {
  const nav = useNavigate()
  const [searchParams] = useSearchParams()
  const fileRef = useRef<HTMLInputElement>(null)

  const requestedVoiceSlug = searchParams.get('voice') || ''

  const [voices, setVoices] = useState<Voice[]>([])
  const [name, setName] = useState('')
  const [voiceRef, setVoiceRef] = useState('')
  const [book, setBook] = useState<File | null>(null)
  const [language, setLanguage] = useState('cs')
  const [customLanguage, setCustomLanguage] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [dragActive, setDragActive] = useState(false)

  useEffect(() => {
    listVoices()
      .then((vs) => {
        setVoices(vs)
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

  const isTxt = book?.name.toLowerCase().endsWith('.txt') ?? false
  const effectiveLanguage =
    language === 'other' ? customLanguage.trim().toLowerCase() : language
  const txtLangValid = !isTxt || /^[a-z]{2,3}(-[a-zA-Z]{2,4})?$/.test(effectiveLanguage)

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
