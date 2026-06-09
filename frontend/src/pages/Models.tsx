import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  ArrowLeft,
  Cpu,
  Cloud,
  Download,
  FolderInput,
  HardDrive,
  Library,
  Lock,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Badge } from '@/components/ui/badge'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useConfirm } from '@/components/ConfirmDialog'
import { InlineProgressCard } from '@/components/InlineProgressCard'
import {
  deleteModel,
  downloadHFModel,
  getHFTokenStatus,
  listModels,
  listMyHFRepos,
  listVoices,
  redownloadModel,
  registerLocalModel,
} from '@/lib/api'
import type { HFRepoInfo, HFTokenStatus, TTSModel, Voice } from '@/lib/types'

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${(n / 1024 ** 3).toFixed(2)} GB`
}

export function Models() {
  const nav = useNavigate()
  const [models, setModels] = useState<TTSModel[]>([])
  const [voices, setVoices] = useState<Voice[]>([])
  const [hfStatus, setHfStatus] = useState<HFTokenStatus | null>(null)
  const [err, setErr] = useState('')
  const [addLocalOpen, setAddLocalOpen] = useState(false)
  const [addHFOpen, setAddHFOpen] = useState(false)
  const { confirm, dialog: confirmDialog } = useConfirm()

  const refresh = () => {
    Promise.all([listModels(), listVoices()])
      .then(([m, v]) => {
        setModels(m)
        setVoices(v)
      })
      .catch((e) => setErr(String(e)))
  }

  useEffect(() => {
    refresh()
    getHFTokenStatus().then(setHfStatus).catch(() => {})
  }, [])

  // Map model slug → voices that currently reference it. Used both in
  // the ModelRow "X voices using" badge and in the delete-confirm copy.
  const voicesByModel = useMemo(() => {
    const map = new Map<string, Voice[]>()
    for (const v of voices) {
      if (!v.tts_model) continue
      if (!map.has(v.tts_model)) map.set(v.tts_model, [])
      map.get(v.tts_model)!.push(v)
    }
    return map
  }, [voices])

  const onDelete = (m: TTSModel) => {
    const users = voicesByModel.get(m.name_slug) ?? []
    const usersNote =
      users.length > 0
        ? ` ${users.length} voice(s) currently reference this model (${users.map((v) => v.name).join(', ')}) — they will fall back to stock OmniVoice on next render.`
        : ''
    confirm({
      title: `Delete model "${m.name}"?`,
      description: `Removes ${formatBytes(m.size_bytes)} of files from the registry.${usersNote}`,
      confirmText: 'Delete',
      destructive: true,
      onConfirm: async () => {
        try {
          await deleteModel(m.name_slug)
          refresh()
        } catch (e) {
          setErr(String(e))
        }
      },
    })
  }

  const onRedownload = (m: TTSModel) => {
    if (m.source_type !== 'hf') return
    confirm({
      title: `Re-download "${m.name}"?`,
      description: `Pulls the same revision from ${m.source_ref} again, overwriting local files. Useful if local copy got corrupted.`,
      confirmText: 'Re-download',
      onConfirm: async () => {
        try {
          await redownloadModel(m.name_slug)
          refresh()
        } catch (e) {
          setErr(String(e))
        }
      },
    })
  }

  return (
    <div className="max-w-4xl space-y-6">
      <div>
        <Button variant="ghost" onClick={() => nav('/')} className="-ml-3">
          <ArrowLeft className="h-4 w-4" />
          Back
        </Button>
        <h1 className="text-2xl font-bold mt-2">TTS models</h1>
        <p className="text-sm text-muted-foreground">
          Registered model checkpoints (local fine-tunes + HF-sourced snapshots).
          Each voice can opt in to a registered model under{' '}
          <a href="/voices" className="text-primary underline">Voices</a>; voices
          without a model use the stock <code>k2-fsa/OmniVoice</code>.
        </p>
      </div>

      {err && (
        <div className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-destructive">
          {err}
        </div>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <CardTitle>Registered models ({models.length})</CardTitle>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setAddLocalOpen(true)}>
              <FolderInput className="h-4 w-4" />
              Add local
            </Button>
            <Button onClick={() => setAddHFOpen(true)}>
              <Cloud className="h-4 w-4" />
              Add from HF
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="rounded-md border bg-secondary/40 p-3 text-sm flex items-center gap-2">
            <Cpu className="h-4 w-4 text-muted-foreground" />
            <span className="text-muted-foreground">Stock fallback:</span>
            <code className="font-mono">k2-fsa/OmniVoice</code>
            <span className="text-muted-foreground">
              · pulled from HF on demand · always available
            </span>
          </div>

          {models.length === 0 ? (
            <p className="text-sm text-muted-foreground italic">
              No user-registered models yet. Use the buttons above to add a
              local fine-tune or pull from Hugging Face.
            </p>
          ) : (
            <div className="space-y-2">
              {models.map((m) => (
                <ModelRow
                  key={m.name_slug}
                  model={m}
                  usedBy={voicesByModel.get(m.name_slug) ?? []}
                  onDelete={() => onDelete(m)}
                  onRedownload={() => onRedownload(m)}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <AddLocalModelDialog
        open={addLocalOpen}
        onClose={() => setAddLocalOpen(false)}
        onAdded={() => {
          setAddLocalOpen(false)
          refresh()
        }}
      />

      <AddHFModelDialog
        open={addHFOpen}
        onClose={() => setAddHFOpen(false)}
        onAdded={() => {
          setAddHFOpen(false)
          refresh()
        }}
        hfStatus={hfStatus}
      />

      {confirmDialog}
    </div>
  )
}

function ModelRow({
  model,
  usedBy,
  onDelete,
  onRedownload,
}: {
  model: TTSModel
  usedBy: Voice[]
  onDelete: () => void
  onRedownload: () => void
}) {
  const isHF = model.source_type === 'hf'
  // v0.5: badge uses the capability's `short_label` so any future
  // engine that isn't the default OmniVoice gets a consistent chip
  // without us hand-writing a new `isXyz` literal here.
  const showEngineBadge = model.capabilities.short_label !== 'OmniVoice'
  const isNonCommercial = model.license === 'non_commercial'
  return (
    <div className="rounded-md border bg-card p-3 flex items-start justify-between gap-3">
      <div className="space-y-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium">{model.name}</span>
          <Badge variant="secondary" className="font-normal">
            {isHF ? (
              <>
                <Cloud className="h-3 w-3" /> HF
              </>
            ) : (
              <>
                <HardDrive className="h-3 w-3" /> local
              </>
            )}
          </Badge>
          {showEngineBadge && (
            <Badge
              variant="default"
              className="font-normal"
              title={`${model.capabilities.display_name} backend`}
            >
              {model.capabilities.short_label}
            </Badge>
          )}
          {isNonCommercial ? (
            <Badge
              variant="outline"
              className="font-normal border-amber-500/60 text-amber-700 dark:text-amber-300 bg-amber-500/10"
              title="Model weights ship under a non-commercial license. Audiomat itself stays MIT."
            >
              <AlertTriangle className="h-3 w-3" />
              non-commercial
            </Badge>
          ) : (
            <Badge
              variant="outline"
              className="font-normal text-xs"
              title="Permissive license (typically Apache-2.0 or MIT)"
            >
              <ShieldCheck className="h-3 w-3" />
              permissive
            </Badge>
          )}
          <span className="text-xs text-muted-foreground">
            {formatBytes(model.size_bytes)}
          </span>
          {usedBy.length > 0 && (
            <Badge variant="outline" className="font-normal text-xs">
              <Library className="h-3 w-3" />
              {usedBy.length} voice{usedBy.length === 1 ? '' : 's'}
            </Badge>
          )}
        </div>
        <div className="text-xs text-muted-foreground font-mono break-all">
          {isHF
            ? `${model.source_ref}${model.hf_revision ? ` @ ${model.hf_revision.slice(0, 12)}` : ''}`
            : model.source_ref}
        </div>
        {model.notes && (
          <p className="text-xs text-muted-foreground italic">{model.notes}</p>
        )}
        {usedBy.length > 0 && (
          <p className="text-xs text-muted-foreground">
            Used by:{' '}
            {usedBy.map((v, i) => (
              <span key={v.name_slug}>
                {i > 0 && ', '}
                <Link
                  to="/voices"
                  className="text-foreground hover:underline"
                  title="Manage on Voices page"
                >
                  {v.name}
                </Link>
              </span>
            ))}
          </p>
        )}
      </div>
      <div className="flex gap-1 shrink-0">
        {isHF && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onRedownload}
            title="Re-pull from Hugging Face"
          >
            <RefreshCw className="h-4 w-4" />
          </Button>
        )}
        <Button
          variant="ghost"
          size="sm"
          onClick={onDelete}
          className="text-destructive hover:bg-destructive/10 hover:text-destructive"
          title="Delete model"
        >
          <Trash2 className="h-4 w-4" />
        </Button>
      </div>
    </div>
  )
}

// ----------------------------------------------------------------------------
// Add-local dialog
// ----------------------------------------------------------------------------

function AddLocalModelDialog({
  open,
  onClose,
  onAdded,
}: {
  open: boolean
  onClose: () => void
  onAdded: () => void
}) {
  const [name, setName] = useState('')
  const [srcDir, setSrcDir] = useState('')
  const [notes, setNotes] = useState('')
  const [backend, setBackend] = useState<'omnivoice' | 'higgs'>('omnivoice')
  const [license, setLicense] = useState<'permissive' | 'non_commercial'>('permissive')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const onSubmit = async () => {
    if (!name.trim() || !srcDir.trim()) return
    setBusy(true)
    setErr('')
    try {
      await registerLocalModel({
        name: name.trim(),
        src_dir: srcDir.trim(),
        notes: notes.trim(),
        backend,
        license,
      })
      setName('')
      setSrcDir('')
      setNotes('')
      setBackend('omnivoice')
      setLicense('permissive')
      onAdded()
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add local TTS model</DialogTitle>
          <DialogDescription>
            Copy a checkpoint directory into the registry. The source must
            contain at least <code>config.json</code> + the weight files
            (safetensors / .bin).
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label htmlFor="ml-name">Display name</Label>
            <Input
              id="ml-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Jitka Ježková v1"
            />
            <p className="text-xs text-muted-foreground">
              Will be slugified for the on-disk dir name (e.g. <code>Jitka_Jezkova_v1</code>).
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="ml-src">Source directory (container path)</Label>
            <Input
              id="ml-src"
              value={srcDir}
              onChange={(e) => setSrcDir(e.target.value)}
              placeholder="/data/uploads/jezkova-v1"
              className="font-mono"
            />
            <p className="text-xs text-muted-foreground">
              Path as seen from inside the audiomat container. Bind-mount your
              fine-tune output dir, e.g.
              <code className="block mt-1">
                -v /host/training/output/checkpoint-1500:/data/uploads/jezkova-v1
              </code>
            </p>
          </div>

          <BackendLicensePicker
            backend={backend} setBackend={setBackend}
            license={license} setLicense={setLicense}
            disabled={busy}
          />

          <div className="space-y-2">
            <Label htmlFor="ml-notes">Notes (optional)</Label>
            <Textarea
              id="ml-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Training metadata, data origin, dates…"
              rows={2}
            />
          </div>

          {err && <p className="text-xs text-destructive">{err}</p>}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={onSubmit} disabled={!name.trim() || !srcDir.trim() || busy}>
            <Plus className="h-4 w-4" />
            {busy ? 'Adding…' : 'Add to registry'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ----------------------------------------------------------------------------
// Shared backend + license picker used by both Add dialogs. Surfaces the
// non-commercial obligation prominently when the user picks a license
// flag that requires it.
// ----------------------------------------------------------------------------

function BackendLicensePicker({
  backend, setBackend, license, setLicense, disabled,
}: {
  backend: 'omnivoice' | 'higgs'
  setBackend: (v: 'omnivoice' | 'higgs') => void
  license: 'permissive' | 'non_commercial'
  setLicense: (v: 'permissive' | 'non_commercial') => void
  disabled?: boolean
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      <div className="space-y-2">
        <Label htmlFor="ml-backend">Backend</Label>
        <select
          id="ml-backend"
          className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
          value={backend}
          onChange={(e) => {
            const next = e.target.value as 'omnivoice' | 'higgs'
            setBackend(next)
            // Convenience: Higgs ships under a non-commercial license, so
            // auto-flip the license flag when the user picks Higgs. They
            // can still override before submit.
            if (next === 'higgs' && license === 'permissive') {
              setLicense('non_commercial')
            }
          }}
          disabled={disabled}
        >
          <option value="omnivoice">OmniVoice (Apache-2.0)</option>
          <option value="higgs">Higgs Audio v3 (multimodalart port)</option>
        </select>
        <p className="text-xs text-muted-foreground">
          Selects the TTS adapter the renderer uses at runtime. Higgs needs
          ~8.6 GB VRAM (bf16).
        </p>
      </div>
      <div className="space-y-2">
        <Label htmlFor="ml-license">License</Label>
        <select
          id="ml-license"
          className="flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm"
          value={license}
          onChange={(e) =>
            setLicense(e.target.value as 'permissive' | 'non_commercial')
          }
          disabled={disabled}
        >
          <option value="permissive">Permissive (Apache / MIT)</option>
          <option value="non_commercial">Non-commercial use only</option>
        </select>
        {license === 'non_commercial' && (
          <p className="text-xs text-amber-600 dark:text-amber-400 flex items-start gap-1">
            <AlertTriangle className="h-3 w-3 mt-0.5 shrink-0" />
            You agree these weights are used for non-commercial purposes
            only. Audiomat code stays MIT.
          </p>
        )}
      </div>
    </div>
  )
}

// ----------------------------------------------------------------------------
// Add-from-HF dialog
// ----------------------------------------------------------------------------

function AddHFModelDialog({
  open,
  onClose,
  onAdded,
  hfStatus,
}: {
  open: boolean
  onClose: () => void
  onAdded: () => void
  hfStatus: HFTokenStatus | null
}) {
  const [name, setName] = useState('')
  const [repoId, setRepoId] = useState('')
  const [revision, setRevision] = useState('')
  const [notes, setNotes] = useState('')
  const [backend, setBackend] = useState<'omnivoice' | 'higgs'>('omnivoice')
  const [license, setLicense] = useState<'permissive' | 'non_commercial'>('permissive')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [percent, setPercent] = useState<number | null>(null)
  const [totalBytes, setTotalBytes] = useState(0)
  const [doneBytes, setDoneBytes] = useState(0)

  // "Browse my HF models" picker
  const [browsing, setBrowsing] = useState(false)
  const [myRepos, setMyRepos] = useState<HFRepoInfo[] | null>(null)

  const onBrowse = async () => {
    setBrowsing(true)
    setErr('')
    try {
      const repos = await listMyHFRepos()
      setMyRepos(repos)
    } catch (e) {
      setErr(String(e))
    } finally {
      setBrowsing(false)
    }
  }

  const onPickRepo = (repo: HFRepoInfo) => {
    setRepoId(repo.repo_id)
    if (!name) setName(repo.repo_id.split('/').pop() ?? '')
    setMyRepos(null)
  }

  const onSubmit = async () => {
    if (!name.trim() || !repoId.trim()) return
    setBusy(true)
    setErr('')
    setPercent(0)
    setDoneBytes(0)
    setTotalBytes(0)
    try {
      await downloadHFModel(
        {
          name: name.trim(),
          repo_id: repoId.trim(),
          revision: revision.trim() || null,
          notes: notes.trim(),
          backend,
          license,
        },
        {
          onStarted: (total) => setTotalBytes(total),
          onProgress: (down, total, pct) => {
            setDoneBytes(down)
            setTotalBytes(total)
            setPercent(pct)
          },
        },
      )
      // Reset + close on success
      setName('')
      setRepoId('')
      setRevision('')
      setNotes('')
      setBackend('omnivoice')
      setLicense('permissive')
      setPercent(null)
      onAdded()
    } catch (e) {
      setErr(String(e))
      setPercent(null)
    } finally {
      setBusy(false)
    }
  }

  const sizeNote =
    totalBytes > 0
      ? `${formatBytes(doneBytes)} / ${formatBytes(totalBytes)}`
      : `${formatBytes(doneBytes)} (unknown total)`

  return (
    <Dialog open={open} onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add from Hugging Face</DialogTitle>
          <DialogDescription>
            Pull a model snapshot from HF into the local registry. Token comes
            from{' '}
            <a href="/settings" className="text-primary underline">
              Settings → HF token
            </a>
            {hfStatus?.has_token ? (
              <>
                {' '}(currently {hfStatus.source === 'env' ? 'env var' : 'stored'}).
              </>
            ) : (
              <>
                {' '}— <strong>not configured</strong>. Public repos work
                anonymously but hit rate limits faster; private repos won't load.
              </>
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <div className="space-y-2">
            <Label htmlFor="hf-name">Display name</Label>
            <Input
              id="hf-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Jitka Ježková v1"
              disabled={busy}
            />
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <Label htmlFor="hf-repo">HF repo ID</Label>
              {hfStatus?.has_token && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={onBrowse}
                  disabled={busy || browsing}
                  className="h-7 text-xs"
                >
                  {browsing ? 'Loading…' : 'Browse my HF models'}
                </Button>
              )}
            </div>
            <Input
              id="hf-repo"
              value={repoId}
              onChange={(e) => setRepoId(e.target.value)}
              placeholder="username/repo-name"
              className="font-mono"
              disabled={busy}
            />
          </div>

          {myRepos !== null && (
            <div className="rounded-md border bg-secondary/40 p-2 max-h-48 overflow-y-auto space-y-1">
              {myRepos.length === 0 ? (
                <p className="text-xs text-muted-foreground italic p-2">
                  No models found on your HF account.
                </p>
              ) : (
                myRepos.map((r) => (
                  <button
                    key={r.repo_id}
                    type="button"
                    onClick={() => onPickRepo(r)}
                    className="w-full text-left p-2 rounded hover:bg-accent text-sm flex items-center justify-between gap-2"
                  >
                    <span className="font-mono truncate">{r.repo_id}</span>
                    <span className="flex items-center gap-2 shrink-0 text-xs text-muted-foreground">
                      {r.private && <Lock className="h-3 w-3" />}
                      {r.size_bytes > 0 && formatBytes(r.size_bytes)}
                    </span>
                  </button>
                ))
              )}
            </div>
          )}

          <div className="space-y-2">
            <Label htmlFor="hf-rev">Revision (optional)</Label>
            <Input
              id="hf-rev"
              value={revision}
              onChange={(e) => setRevision(e.target.value)}
              placeholder="main, branch name, or commit SHA"
              className="font-mono"
              disabled={busy}
            />
            <p className="text-xs text-muted-foreground">
              Leave empty for the default branch. We pin the actual commit SHA
              at download time so re-downloads stay reproducible.
            </p>
          </div>

          <BackendLicensePicker
            backend={backend} setBackend={setBackend}
            license={license} setLicense={setLicense}
            disabled={busy}
          />

          <div className="space-y-2">
            <Label htmlFor="hf-notes">Notes (optional)</Label>
            <Textarea
              id="hf-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              disabled={busy}
            />
          </div>

          {busy && percent != null && (
            <InlineProgressCard
              message={`Downloading from ${repoId} — ${sizeNote}`}
              percent={percent}
            />
          )}
          {err && <p className="text-xs text-destructive">{err}</p>}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            onClick={onSubmit}
            disabled={!name.trim() || !repoId.trim() || busy}
          >
            <Download className="h-4 w-4" />
            {busy ? 'Downloading…' : 'Download'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
