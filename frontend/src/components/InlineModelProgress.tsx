import { InlineProgressCard } from '@/components/InlineProgressCard'
import { useModelStatus } from '@/lib/useModelStatus'

/**
 * Compact in-context download progress for places where the user just
 * clicked an action button. The global SystemBanner up top is fine for
 * "something's loading somewhere" but the user's eye stays on the
 * button they pressed — render this right below it so they don't think
 * the click is hung.
 *
 * Self-hides when model is ready or unloaded. Pass ``visible`` to gate
 * on the local busy state so it doesn't show when the model is still
 * downloading but the user hasn't actually clicked anything yet.
 */
export function InlineModelProgress({ visible = true }: { visible?: boolean }) {
  // While the parent is busy, force fast (500 ms) polling so we catch the
  // brief load-from-warm-cache transition (~5 s) instead of missing it
  // between 10 s idle polls.
  const status = useModelStatus(visible)
  if (!visible) return null
  if (!status) return null
  if (status.state !== 'downloading' && status.state !== 'loading') return null
  return (
    <InlineProgressCard
      message={status.message ?? 'Připravuji TTS model…'}
      percent={status.percent}
    />
  )
}
