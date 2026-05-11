import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ArrowLeft, Check, Eye, EyeOff, KeyRound, Save, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  clearHFToken,
  getHFTokenStatus,
  setHFToken,
  validateHFToken,
} from '@/lib/api'
import type { HFTokenStatus } from '@/lib/types'

export function Settings() {
  const nav = useNavigate()
  const [status, setStatus] = useState<HFTokenStatus | null>(null)
  const [pending, setPending] = useState('')
  const [showToken, setShowToken] = useState(false)
  const [busy, setBusy] = useState(false)
  const [validating, setValidating] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const refresh = () =>
    getHFTokenStatus()
      .then(setStatus)
      .catch((e) => setErr(String(e)))

  useEffect(() => {
    refresh()
  }, [])

  const onSave = async () => {
    const t = pending.trim()
    if (!t) return
    setBusy(true)
    setErr('')
    setMsg('')
    try {
      const next = await setHFToken(t)
      setStatus(next)
      setPending('')
      setMsg('Token saved.')
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const onClear = async () => {
    setBusy(true)
    setErr('')
    setMsg('')
    try {
      const next = await clearHFToken()
      setStatus(next)
      setMsg('Stored token cleared.')
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const onValidate = async () => {
    const t = pending.trim()
    if (!t) return
    setValidating(true)
    setErr('')
    setMsg('')
    try {
      const result = await validateHFToken(t)
      setMsg(`✓ valid — sees ${result.repo_count} repo(s)`)
    } catch (e) {
      setErr(String(e))
    } finally {
      setValidating(false)
    }
  }

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <Button variant="ghost" onClick={() => nav('/')} className="-ml-3">
          <ArrowLeft className="h-4 w-4" />
          Back
        </Button>
        <h1 className="text-2xl font-bold mt-2">Settings</h1>
        <p className="text-sm text-muted-foreground">
          App-wide configuration. Currently just the Hugging Face token used for
          downloading TTS models from private HF repos.
        </p>
      </div>

      {err && (
        <div className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
          {err}
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="h-5 w-5" />
            Hugging Face token
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Required to download from private HF repos (your own fine-tunes, or
            ones shared with you as a collaborator). Get a token at{' '}
            <a
              href="https://huggingface.co/settings/tokens"
              target="_blank"
              rel="noreferrer"
              className="text-primary underline"
            >
              huggingface.co/settings/tokens
            </a>{' '}
            — <code>Read</code> scope is enough for downloads. <code>Write</code>{' '}
            also needed if you plan to publish models from audiomat later.
          </p>

          <div className="rounded-md border bg-secondary/40 p-3 text-sm">
            <p className="font-medium">
              Current:{' '}
              {!status ? (
                <span className="text-muted-foreground">checking…</span>
              ) : status.has_token ? (
                <span className="text-foreground">
                  ✓ configured
                  {status.source === 'env' && (
                    <span className="text-muted-foreground">
                      {' '}— using <code>HF_TOKEN</code> env var (overrides stored
                      value)
                    </span>
                  )}
                  {status.source === 'secrets_file' && (
                    <span className="text-muted-foreground">
                      {' '}— stored in <code>secrets.json</code>
                    </span>
                  )}
                </span>
              ) : (
                <span className="text-muted-foreground">not configured</span>
              )}
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="hf-token">
              {status?.has_token ? 'Replace token' : 'New token'}
            </Label>
            <div className="flex gap-2">
              <Input
                id="hf-token"
                type={showToken ? 'text' : 'password'}
                value={pending}
                onChange={(e) => setPending(e.target.value)}
                placeholder="hf_..."
                className="font-mono"
                autoComplete="off"
              />
              <Button
                type="button"
                variant="outline"
                size="icon"
                onClick={() => setShowToken((s) => !s)}
                title={showToken ? 'Hide' : 'Show'}
              >
                {showToken ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Stored at <code>~/audiomat/secrets.json</code> with user-only file
              perms. Never sent back to the UI.
            </p>
            {msg && <p className="text-xs text-foreground">{msg}</p>}
          </div>

          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={onValidate}
              disabled={!pending.trim() || validating || busy}
            >
              <Check className="h-4 w-4" />
              {validating ? 'Validating…' : 'Validate'}
            </Button>
            <Button onClick={onSave} disabled={!pending.trim() || busy}>
              <Save className="h-4 w-4" />
              {busy ? 'Saving…' : 'Save'}
            </Button>
            {status?.has_token && status.source === 'secrets_file' && (
              <Button
                variant="ghost"
                onClick={onClear}
                disabled={busy}
                className="ml-auto text-destructive hover:bg-destructive/10 hover:text-destructive"
              >
                <Trash2 className="h-4 w-4" />
                Clear stored
              </Button>
            )}
          </div>

          {status?.source === 'env' && (
            <div className="rounded-md border border-amber-200/70 bg-amber-50/60 dark:border-amber-900/50 dark:bg-amber-950/20 px-3 py-2 text-xs">
              <p>
                The <code>HF_TOKEN</code> environment variable is set and takes
                precedence over anything stored in <code>secrets.json</code>.
                Clear or change <code>HF_TOKEN</code> outside the app (e.g. in
                Docker <code>-e</code> flags) to switch.
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
