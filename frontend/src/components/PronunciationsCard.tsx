/**
 * PronunciationsCard — edit the per-project pronunciation dictionary.
 *
 * Sits on the Advanced tab. The dict is {source: target}; the TTS pipeline
 * substitutes ``source`` with ``target`` (word-boundary, case-sensitive,
 * longest-first) on every chunk before prepare_for_tts. Use case: fix the
 * pronunciation of foreign proper nouns once and have it apply across
 * every chapter — instead of editing every chapter that mentions
 * "München" individually via the chapter text editor.
 *
 * Cache invalidation is automatic via the manifest signature (dict
 * hash is mixed into the per-chunk sig).
 */
import { useEffect, useState } from 'react'
import { Loader2, Plus, Save, Trash2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'

import { getPronunciations, savePronunciations } from '@/lib/api'

interface Row {
  source: string
  target: string
}

interface Props {
  slug: string
}

function mapToRows(m: Record<string, string>): Row[] {
  return Object.entries(m).map(([source, target]) => ({ source, target }))
}

function rowsToMap(rows: Row[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const r of rows) {
    const k = r.source.trim()
    if (!k) continue
    out[k] = r.target
  }
  return out
}

function rowsEqual(a: Record<string, string>, b: Record<string, string>): boolean {
  const ka = Object.keys(a).sort()
  const kb = Object.keys(b).sort()
  if (ka.length !== kb.length) return false
  return ka.every((k, i) => k === kb[i] && a[k] === b[k])
}

export function PronunciationsCard({ slug }: Props) {
  const [rows, setRows] = useState<Row[]>([])
  const [saved, setSaved] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    setLoading(true)
    setErr(null)
    getPronunciations(slug)
      .then((m) => {
        setSaved(m)
        setRows(mapToRows(m))
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [slug])

  const current = rowsToMap(rows)
  const dirty = !rowsEqual(current, saved)
  const hasEmptySource = rows.some((r) => r.target.trim() && !r.source.trim())

  const addRow = () => setRows([...rows, { source: '', target: '' }])
  const removeRow = (i: number) => setRows(rows.filter((_, idx) => idx !== i))
  const updateRow = (i: number, patch: Partial<Row>) =>
    setRows(rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)))

  const onSave = async () => {
    setSaving(true)
    setErr(null)
    try {
      const result = await savePronunciations(slug, current)
      setSaved(result)
      setRows(mapToRows(result))
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  const onDiscard = () => {
    setRows(mapToRows(saved))
    setErr(null)
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between">
          <span>Pronunciations</span>
          <span className="text-xs text-muted-foreground font-normal">
            {Object.keys(saved).length} {Object.keys(saved).length === 1 ? 'entry' : 'entries'}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Substitutes <code>source</code> with <code>target</code> on every chunk
          before TTS — word-boundary, case-sensitive, longest match wins. Useful
          for foreign proper nouns ("München" → "Mnichov"), unusual names, or
          numbers you want spelled differently than the default num2words
          expansion. Saving invalidates cached chunks; next render re-synths.
        </p>

        {loading && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading…
          </div>
        )}

        {err && (
          <div className="rounded-md bg-destructive/10 text-destructive p-3 text-sm">
            {err}
          </div>
        )}

        {!loading && (
          <>
            {rows.length === 0 ? (
              <p className="text-sm text-muted-foreground italic">
                No entries yet. Click "Add entry" below.
              </p>
            ) : (
              <div className="space-y-2">
                <div className="grid grid-cols-[1fr_1fr_auto] gap-2 text-xs text-muted-foreground px-1">
                  <span>Source (literal text in book)</span>
                  <span>Target (what TTS reads)</span>
                  <span className="w-8" />
                </div>
                {rows.map((r, i) => (
                  <div
                    key={i}
                    className="grid grid-cols-[1fr_1fr_auto] gap-2 items-start"
                  >
                    <Input
                      value={r.source}
                      onChange={(e) => updateRow(i, { source: e.target.value })}
                      placeholder="München"
                      className="font-mono text-sm"
                    />
                    <Input
                      value={r.target}
                      onChange={(e) => updateRow(i, { target: e.target.value })}
                      placeholder="Mnichov"
                      className="font-mono text-sm"
                    />
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => removeRow(i)}
                      className="h-9 w-9 p-0 text-destructive hover:text-destructive hover:bg-destructive/10"
                      title="Remove entry"
                    >
                      <Trash2 className="h-3 w-3" />
                    </Button>
                  </div>
                ))}
              </div>
            )}

            {hasEmptySource && (
              <p className="text-xs text-amber-600">
                Rows with empty source are skipped on save.
              </p>
            )}

            <div className="flex items-center justify-between gap-3 pt-2 border-t border-dashed">
              <Button variant="outline" size="sm" onClick={addRow}>
                <Plus className="h-4 w-4" />
                Add entry
              </Button>

              <div className="flex items-center gap-2">
                {dirty && (
                  <span className="text-xs text-amber-600">unsaved</span>
                )}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={onDiscard}
                  disabled={!dirty || saving}
                >
                  Discard
                </Button>
                <Button
                  size="sm"
                  onClick={onSave}
                  disabled={!dirty || saving}
                >
                  {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                  Save
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
