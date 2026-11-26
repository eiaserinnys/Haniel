import * as Dialog from '@radix-ui/react-dialog'
import { useEffect, useState } from 'react'
import { useOpener } from '@/hooks/useOpener'
import type { SelfUpdateResult } from '@/lib/types'

const UPDATE_TIMEOUT_MS = 120_000

interface UpdateOverlayProps {
  result: SelfUpdateResult | null
  onDismiss: () => void
}

export function UpdateOverlay({ result, onDismiss }: UpdateOverlayProps) {
  const [timedOut, setTimedOut] = useState(false)
  const returnFocus = useOpener()

  useEffect(() => {
    // Only count down while still waiting. Once a result arrives, useServices
    // either reloads (ok) or surfaces an error (not ok) and the overlay closes.
    if (result !== null) return
    const t = window.setTimeout(() => setTimedOut(true), UPDATE_TIMEOUT_MS)
    return () => window.clearTimeout(t)
  }, [result])

  return (
    <Dialog.Root open onOpenChange={(open) => !open && timedOut && onDismiss()}>
      <Dialog.Portal>
        <Dialog.Overlay className="scrim" />
        <Dialog.Content
          className="status-panel"
          role={timedOut ? 'alertdialog' : 'status'}
          aria-describedby={undefined}
          onCloseAutoFocus={returnFocus}
          onEscapeKeyDown={(e) => !timedOut && e.preventDefault()}
          onPointerDownOutside={(e) => e.preventDefault()}
          onInteractOutside={(e) => e.preventDefault()}
        >
          <span className={`big-marker${timedOut ? ' is-fail' : ''}`} aria-hidden="true" />
          <Dialog.Title className="dialog-title">{timedOut ? 'Server not responding' : 'Updating Haniel'}</Dialog.Title>
          <p className="empty-text">{timedOut ? 'No answer for 120s.' : 'The page reloads when it is back.'}</p>
          {timedOut && (
            <button type="button" className="btn btn-primary" autoFocus onClick={() => window.location.reload()}>
              Refresh
            </button>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
