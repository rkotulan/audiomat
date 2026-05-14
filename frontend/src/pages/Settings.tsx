import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle, ArrowLeft, Check, Download, Eye, EyeOff, KeyRound,
  Loader2, Package, Save, Trash2, Upload,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useConfirm } from '@/components/ConfirmDialog'
import {
  backupDownloadUrl,
  backupPreview,
  backupRestore,
  clearHFToken,
  getHFTokenStatus,
  setHFToken,
  validateHFToken,
} from '@/lib/api'
import type { BackupSize, HFTokenStatus, RestoreResult } from '@/lib/types'

export function Settings() {
  const nav = useNavigate()
  const { confirm, dialog: confirmDialog } = useConfirm()
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
      {confirmDialog}
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

      <BackupCard confirm={confirm} />
    </div>
  )
}

// ----------------------------------------------------------------------------
// Backup / restore card — sits below the HF token section. Lets the
// user download a ZIP of the library (with toggleable scope) and
// upload one to restore. Restore is destructive so it goes through the
// shared useConfirm() dialog.
// ----------------------------------------------------------------------------

function BackupCard({ confirm }: {
  confirm: ReturnType<typeof useConfirm>['confirm']
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [size, setSize] = useState<BackupSize | null>(null)
  const [includeRenders, setIncludeRenders] = useState(false)
  const [includeFinals, setIncludeFinals] = useState(false)
  const [restoring, setRestoring] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  useEffect(() => {
    backupPreview()
      .then(setSize)
      .catch((e) => setErr(String(e)))
  }, [])

  const totalBytes = !size ? 0 :
    size.essentials_bytes
    + (includeRenders ? size.renders_bytes : 0)
    + (includeFinals ? size.finals_bytes : 0)

  const onRestoreClick = (file: File) => {
    confirm({
      title: 'Restore from backup?',
      description: (
        `This will REPLACE your current library (${file.name}, ` +
        `${fmtBytes(file.size)}). A safety snapshot of your current ` +
        `state's essentials will be saved next to the library root ` +
        `before extraction. The HF model cache is preserved.`
      ),
      confirmText: 'Replace & restore',
      destructive: true,
      onConfirm: async () => {
        setRestoring(true)
        setMsg('')
        setErr('')
        try {
          const r: RestoreResult = await backupRestore(file)
          const snap = r.pre_restore_snapshot
            ? ` Safety snapshot at: ${r.pre_restore_snapshot}.`
            : ''
          setMsg(
            `✓ Restored ${r.files_extracted} files ` +
            `(${fmtBytes(r.bytes_extracted)}).${snap} ` +
            `Refresh the page or restart the server to pick up the new state.`,
          )
          backupPreview().then(setSize).catch(() => {})
        } catch (e) {
          setErr(String(e))
        } finally {
          setRestoring(false)
        }
      },
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Package className="h-5 w-5" />
          Backup &amp; restore
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {err && (
          <div className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
            {err}
          </div>
        )}
        {msg && (
          <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-sm">
            {msg}
          </div>
        )}

        <div>
          <Label className="text-xs uppercase tracking-wide text-muted-foreground">
            Download
          </Label>
          <p className="text-sm text-muted-foreground mt-1">
            Essentials are always included: DB, settings, secrets, voice
            clips, book sources, per-chapter overrides. Toggle the optional
            tiers below — finals + renders make the backup self-sufficient
            but can be many GB.
          </p>

          <div className="mt-3 space-y-2 rounded-md border bg-secondary/30 p-3 text-sm">
            <ScopeRow
              label="Essentials"
              hint="DB + voices + books + overrides"
              bytes={size?.essentials_bytes}
              files={size?.file_counts.essentials}
              checked
              disabled
            />
            <ScopeRow
              label="Rendered chunks"
              hint="per-chapter chunk WAVs + concat WAVs"
              bytes={size?.renders_bytes}
              files={size?.file_counts.renders}
              checked={includeRenders}
              onChange={setIncludeRenders}
            />
            <ScopeRow
              label="Final M4Bs"
              hint="final.m4b per project"
              bytes={size?.finals_bytes}
              files={size?.file_counts.finals}
              checked={includeFinals}
              onChange={setIncludeFinals}
            />
            <div className="border-t pt-2 flex items-center justify-between text-xs">
              <span className="text-muted-foreground">Total download</span>
              <span className="font-mono font-medium">{fmtBytes(totalBytes)}</span>
            </div>
          </div>

          <div className="mt-3">
            <Button asChild disabled={!size || totalBytes === 0}>
              <a
                href={backupDownloadUrl({
                  includeRenders,
                  includeFinals,
                })}
              >
                <Download className="h-4 w-4" />
                Download backup
              </a>
            </Button>
          </div>
        </div>

        <div className="border-t pt-4">
          <Label className="text-xs uppercase tracking-wide text-muted-foreground">
            Restore
          </Label>
          <p className="text-sm text-muted-foreground mt-1">
            Upload a previous backup ZIP to replace the current library.
            We auto-snapshot your current state's essentials beside the
            library before wiping, so you can roll back manually if the
            wrong file was uploaded.
          </p>
          <div className="mt-3 rounded-md border border-amber-200/70 bg-amber-50/60 dark:border-amber-900/50 dark:bg-amber-950/20 p-3 text-xs flex items-start gap-2">
            <AlertTriangle className="h-4 w-4 mt-0.5 flex-shrink-0 text-amber-600 dark:text-amber-400" />
            <p>
              Restore is destructive. Any voices / projects not in the
              uploaded backup will be removed. The HF model cache
              (<code>cache/</code>) is preserved.
            </p>
          </div>
          <div className="mt-3">
            <input
              ref={fileRef}
              type="file"
              accept=".zip,application/zip"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) onRestoreClick(f)
                if (e.target) e.target.value = ''
              }}
            />
            <Button
              variant="outline"
              onClick={() => fileRef.current?.click()}
              disabled={restoring}
            >
              {restoring ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
              {restoring ? 'Restoring…' : 'Choose backup ZIP…'}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function ScopeRow({
  label, hint, bytes, files, checked, disabled, onChange,
}: {
  label: string
  hint: string
  bytes?: number
  files?: number
  checked: boolean
  disabled?: boolean
  onChange?: (next: boolean) => void
}) {
  return (
    <label className="flex items-center gap-3 cursor-pointer">
      <Checkbox
        checked={checked}
        disabled={disabled}
        onCheckedChange={
          onChange ? (v) => onChange(v === true) : undefined
        }
      />
      <div className="flex-1">
        <p className="font-medium text-sm">{label}</p>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </div>
      <div className="text-right text-xs font-mono">
        <p>{bytes !== undefined ? fmtBytes(bytes) : '—'}</p>
        <p className="text-muted-foreground">
          {files !== undefined ? `${files} file${files === 1 ? '' : 's'}` : ''}
        </p>
      </div>
    </label>
  )
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  const kb = n / 1024
  if (kb < 1024) return `${kb.toFixed(1)} KB`
  const mb = kb / 1024
  if (mb < 1024) return `${mb.toFixed(1)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}
