import { useEffect, useState } from 'react'

export interface ModelStatus {
  state: 'unloaded' | 'downloading' | 'loading' | 'ready'
  cache_bytes: number
  cache_target_bytes: number
  percent: number
  message: string | null
}

const POLL_MS_FAST = 500     // user is actively waiting on a button
const POLL_MS_ACTIVE = 2000  // download / load is in progress
const POLL_MS_IDLE = 10000   // nothing happening, save bandwidth

/**
 * Polls /api/system/model-status. Adapts cadence:
 *
 * - ``forceFast=true`` → 500 ms (use when the user just clicked an
 *   action button and we need to catch the brief load-from-warm-cache
 *   transition that lasts ~5 s; at idle 10 s polling we'd miss it)
 * - state active (downloading/loading) → 2 s (smooth percent updates
 *   during the ~6 min cold download)
 * - otherwise → 10 s (idle background tick)
 */
export function useModelStatus(forceFast = false): ModelStatus | null {
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
        const isActive =
          next.state === 'downloading' || next.state === 'loading'
        const wait = forceFast
          ? POLL_MS_FAST
          : isActive
          ? POLL_MS_ACTIVE
          : POLL_MS_IDLE
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
  }, [forceFast])

  return status
}

export function isModelBusy(s: ModelStatus | null): boolean {
  return s !== null && (s.state === 'downloading' || s.state === 'loading')
}
