import { Loader2 } from 'lucide-react'
import { Progress } from '@/components/ui/progress'

/**
 * Visual primitive for in-context progress feedback under an action
 * button. Shared by:
 *   - InlineModelProgress (TTS model download / load)
 *   - PreviewTab cell counter (Generating X / 4)
 *
 * Same amber chrome and progress bar so the user reads them as one
 * coherent "something's working" affordance regardless of which
 * subsystem is currently active.
 */
export function InlineProgressCard({
  message,
  percent,
}: {
  message: string
  percent: number
}) {
  const pct = Math.max(0, Math.min(100, Math.round(percent)))
  return (
    <div className="rounded-md border border-amber-200/70 bg-amber-50/60 dark:border-amber-900/50 dark:bg-amber-950/20 px-3 py-2 space-y-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="flex items-center gap-2 text-amber-900 dark:text-amber-200">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {message}
        </span>
        <span className="font-mono text-amber-800 dark:text-amber-300">
          {pct}%
        </span>
      </div>
      <Progress value={pct} className="h-1" />
    </div>
  )
}
