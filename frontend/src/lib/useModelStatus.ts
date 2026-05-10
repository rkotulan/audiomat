import { useEffect, useState } from 'react'

export interface ModelStatus {
  state: 'unloaded' | 'downloading' | 'loading' | 'ready'
  cache_bytes: number
  cache_target_bytes: number
  percent: number
  message: string | null
}

const POLL_MS_ACTIVE = 2000
const POLL_MS_IDLE = 10000

export function useModelStatus(): ModelStatus | null {
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
        const wait =
          next.state === 'ready' || next.state === 'unloaded'
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

  return status
}

export function isModelBusy(s: ModelStatus | null): boolean {
  return s !== null && (s.state === 'downloading' || s.state === 'loading')
}
