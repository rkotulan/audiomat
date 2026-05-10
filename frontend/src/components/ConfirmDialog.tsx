// Reusable confirm dialog hook to replace browser-native window.confirm().
// Usage:
//
//   const { confirm, dialog } = useConfirm()
//   ...
//   onClick={() => confirm({
//     title: 'Reset chapter?',
//     description: 'Deletes cached chunks + final WAV.',
//     confirmText: 'Reset',
//     destructive: true,
//     onConfirm: async () => { ... },
//   })}
//   ...
//   return (<>{dialog}<rest of UI/></>)
//
// One <AlertDialog> instance is rendered per page; state-driven open
// flag and dynamic title/description avoid one instance per call site.

import { useState, useCallback } from 'react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'

export interface ConfirmOptions {
  title: string
  description?: string
  confirmText?: string
  cancelText?: string
  destructive?: boolean
  onConfirm: () => void | Promise<void>
}

interface DialogState extends ConfirmOptions {
  open: boolean
}

export function useConfirm() {
  const [state, setState] = useState<DialogState | null>(null)

  const confirm = useCallback((opts: ConfirmOptions) => {
    setState({ ...opts, open: true })
  }, [])

  const close = useCallback(() => {
    setState((s) => (s ? { ...s, open: false } : s))
    // Drop the snapshot a tick later so the closing animation finishes.
    setTimeout(() => setState(null), 200)
  }, [])

  const dialog = (
    <AlertDialog
      open={state?.open ?? false}
      onOpenChange={(o) => {
        if (!o) close()
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{state?.title ?? ''}</AlertDialogTitle>
          {state?.description && (
            <AlertDialogDescription>{state.description}</AlertDialogDescription>
          )}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>{state?.cancelText ?? 'Cancel'}</AlertDialogCancel>
          <AlertDialogAction
            className={
              state?.destructive
                ? 'bg-destructive text-destructive-foreground hover:bg-destructive/90'
                : undefined
            }
            onClick={async () => {
              const handler = state?.onConfirm
              close()
              if (handler) await handler()
            }}
          >
            {state?.confirmText ?? 'Confirm'}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )

  return { confirm, dialog }
}
