import { useEffect, useState } from 'react'
import { Loader2 } from 'lucide-react'
import { Progress } from '@/components/ui/progress'

interface ModelStatus {
  state: 'unloaded' | 'downloading' | 'loading' | 'ready'
  cache_bytes: number
  cache_target_bytes: number
  percent: number
  message: string | null
}

const POLL_MS_ACTIVE = 2000
const POLL_MS_IDLE = 10000

export function SystemBanner() {
  const [status, setStatus] = useState<ModelStatus | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const r = await fetch('/api/system/model-status')
        if (!r.ok) return
        const next: ModelStatus = await r.json()
        if (cancelled) return
        setStatus(next)
        const wait = next.state === 'ready' || next.state === 'unloaded'
          ? POLL_MS_IDLE
          : POLL_MS_ACTIVE
        timer = window.setTimeout(tick, wait)
      } catch {
        timer = window.setTimeout(tick, POLL_MS_IDLE)
      }
    }

    tick()
    return () => {
      cancelled = true
      if (timer != null) window.clearTimeout(timer)
    }
  }, [])

  if (!status) return null
  if (status.state === 'ready' || status.state === 'unloaded') return null

  const pct = Math.round(status.percent)

  return (
    <div className="border-b bg-amber-50/80 dark:bg-amber-950/30">
      <div className="mx-auto max-w-6xl px-6 py-2.5">
        <div className="flex items-center gap-3 text-sm">
          <Loader2 className="h-4 w-4 animate-spin text-amber-700 dark:text-amber-400" />
          <span className="font-medium text-amber-900 dark:text-amber-100">
            {status.message ?? 'Připravuji TTS model…'}
          </span>
          <span className="ml-auto font-mono text-xs text-amber-800 dark:text-amber-300">
            {pct}%
          </span>
        </div>
        <Progress value={pct} className="mt-1.5 h-1" />
      </div>
    </div>
  )
}
