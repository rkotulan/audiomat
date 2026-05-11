/**
 * ChapterTextEditor — modal for editing a single chapter's text.
 *
 * Shown from the ChaptersList row Pencil button. Backed by GET/PUT/DELETE
 * /api/projects/{slug}/chapters/{stem}/text. The stem is pinned to the
 * EPUB original text on the backend, so the URL stays stable even after
 * an edit changes the leading sentence.
 *
 * UX:
 *  - Auto-pause banner when inject_header_pause would fire at render time.
 *    Surfaces "Header X will get [pause][break] inserted" so the user
 *    knows they don't need to add it manually.
 *  - Toolbar buttons splice [break] / [pause] markers at cursor position.
 *  - Live char count + (when not dirty) estimated chunk count.
 *  - Reset to original removes the override file entirely.
 *  - Cache invalidation is automatic via the manifest signature; saving
 *    flips the chapter's status to pending on the next chapter list refresh.
 */
import { useEffect, useRef, useState } from 'react'
import { Loader2, Trash2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Textarea } from '@/components/ui/textarea'

import { getChapterText, resetChapterText, saveChapterText } from '@/lib/api'
import type { ChapterText } from '@/lib/types'

interface Props {
  open: boolean
  slug: string
  stem: string | null            // null when modal is closing
  onClose: () => void
  onSaved: () => void            // parent refreshes chapter list
}

export function ChapterTextEditor({ open, slug, stem, onClose, onSaved }: Props) {
  const [data, setData] = useState<ChapterText | null>(null)
  const [text, setText] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  // Load chapter text when modal opens with a stem
  useEffect(() => {
    if (!open || !stem) {
      setData(null)
      setText('')
      setErr(null)
      return
    }
    setLoading(true)
    setErr(null)
    getChapterText(slug, stem)
      .then((d) => {
        setData(d)
        setText(d.text)
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [open, slug, stem])

  const dirty = data != null && text !== data.text
  const isOverride = data?.has_override ?? false
  const liveCharCount = text.length

  const insertMarker = (marker: string) => {
    const ta = textareaRef.current
    if (!ta) {
      setText((prev) => prev + marker)
      return
    }
    const start = ta.selectionStart
    const end = ta.selectionEnd
    const next = text.slice(0, start) + marker + text.slice(end)
    setText(next)
    // Restore cursor position after the inserted marker on next tick
    requestAnimationFrame(() => {
      ta.focus()
      ta.setSelectionRange(start + marker.length, start + marker.length)
    })
  }

  const onSave = async () => {
    if (!stem || !text.trim()) return
    setSaving(true)
    setErr(null)
    try {
      const updated = await saveChapterText(slug, stem, text)
      setData(updated)
      setText(updated.text)
      onSaved()
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  const onResetToOriginal = async () => {
    if (!stem) return
    setSaving(true)
    setErr(null)
    try {
      const updated = await resetChapterText(slug, stem)
      setData(updated)
      setText(updated.text)
      onSaved()
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-3xl max-h-[90vh] flex flex-col">
        <DialogHeader>
          <DialogTitle className="font-mono text-sm">
            Edit chapter: {stem ?? ''}
          </DialogTitle>
          <DialogDescription>
            Tweak text in place — fix typos, add manual pauses, override
            pronunciation. Stays per-project; the source EPUB isn't touched.
            Save flips the chapter to pending; next render re-synthesizes.
          </DialogDescription>
        </DialogHeader>

        {loading && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Loading chapter…
          </div>
        )}

        {err && (
          <div className="rounded-md bg-destructive/10 text-destructive p-3 text-sm">
            {err}
          </div>
        )}

        {data && !loading && (
          <>
            {data.auto_pause && (
              <div className="rounded-md bg-secondary/40 p-3 text-xs space-y-1">
                <p className="font-medium">
                  Auto-pause at render time
                </p>
                <p className="text-muted-foreground">
                  Detected{' '}
                  {data.auto_pause.type === 'time_marker'
                    ? 'time marker'
                    : data.auto_pause.type === 'section_header'
                    ? 'section header'
                    : 'header'}
                  {' '}
                  <code className="font-mono">"{data.auto_pause.header}"</code>
                  {' '}— a <code>[pause][break]</code> will be inserted after it
                  automatically. You don't need to add one manually.
                </p>
              </div>
            )}

            <div className="flex flex-wrap gap-2 items-center">
              <span className="text-xs text-muted-foreground mr-1">Insert at cursor:</span>
              <Button
                size="sm"
                variant="outline"
                className="h-7"
                onClick={() => insertMarker('[break]')}
              >
                <code className="text-xs">[break]</code>
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7"
                onClick={() => insertMarker('[pause]')}
              >
                <code className="text-xs">[pause]</code>
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7"
                onClick={() => insertMarker('[pause][break]')}
                title="Stronger pause — pause + sentence-end cue"
              >
                <code className="text-xs">[pause][break]</code>
              </Button>
            </div>

            <Textarea
              ref={textareaRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={18}
              className="font-mono text-sm flex-1 min-h-0"
              placeholder="Chapter text…"
            />

            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>
                {liveCharCount.toLocaleString()} chars
                {!dirty && (
                  <>
                    {' · '}
                    ~{data.estimated_chunks} chunks (between{' '}
                    {data.min_chars}-{data.max_chars} each)
                  </>
                )}
                {dirty && ' · chunk count refreshes after save'}
              </span>
              {isOverride && !dirty && (
                <span className="text-amber-600">override active</span>
              )}
              {dirty && (
                <span className="text-amber-600">unsaved changes</span>
              )}
            </div>
          </>
        )}

        <DialogFooter className="flex-col sm:flex-row gap-2">
          {isOverride && (
            <Button
              variant="ghost"
              onClick={onResetToOriginal}
              disabled={saving}
              className="text-destructive hover:text-destructive hover:bg-destructive/10 sm:mr-auto"
            >
              <Trash2 className="h-4 w-4" />
              Reset to EPUB original
            </Button>
          )}
          <Button variant="outline" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button
            onClick={onSave}
            disabled={!dirty || !text.trim() || saving}
          >
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
