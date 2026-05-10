import { Progress } from '@/components/ui/progress'
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
  const status = useModelStatus()
  if (!visible) return null
  if (!status) return null
  if (status.state !== 'downloading' && status.state !== 'loading') return null
  const pct = Math.round(status.percent)
  return (
    <div className="rounded-md border border-amber-200/70 bg-amber-50/60 dark:border-amber-900/50 dark:bg-amber-950/20 px-3 py-2 space-y-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="text-amber-900 dark:text-amber-200">
          {status.message ?? 'Připravuji TTS model…'}
        </span>
        <span className="font-mono text-amber-800 dark:text-amber-300">
          {pct}%
        </span>
      </div>
      <Progress value={pct} className="h-1" />
    </div>
  )
}
