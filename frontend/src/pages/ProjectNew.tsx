import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { ArrowLeft, BookOpen, Wand2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { createProject, listVoices } from '@/lib/api'
import type { Voice } from '@/lib/types'

export function ProjectNew() {
  const nav = useNavigate()
  const [searchParams] = useSearchParams()
  const fileRef = useRef<HTMLInputElement>(null)

  const requestedVoiceSlug = searchParams.get('voice') || ''

  const [voices, setVoices] = useState<Voice[]>([])
  const [name, setName] = useState('')
  const [voiceRef, setVoiceRef] = useState('')
  const [book, setBook] = useState<File | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    listVoices()
      .then((vs) => {
        setVoices(vs)
        if (voiceRef) return
        // Prefer ?voice=<slug> from query param if it matches a known voice
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

  const canSubmit =
    name.trim() && voiceRef.trim() && book && !busy

  const onSubmit = async () => {
    if (!book) return
    setBusy(true)
    setErr('')
    try {
      const p = await createProject({
        name: name.trim(),
        voice_ref: voiceRef,
        book,
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
              onChange={(e) => setBook(e.target.files?.[0] || null)}
            />
            <Button
              variant="outline"
              onClick={() => fileRef.current?.click()}
              className="w-full justify-start"
            >
              <BookOpen className="h-4 w-4" />
              {book ? book.name : 'Choose EPUB or TXT…'}
            </Button>
            {book && (
              <p className="text-xs text-muted-foreground">
                {(book.size / 1024).toFixed(0)} KB
              </p>
            )}
          </div>

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
