import { useState } from 'react'
import { Modal } from './Modal'
import { Notice } from './states'
import { errorMessage } from '@/lib/utils'

interface ConfirmDialogProps {
  title: string
  sub: React.ReactNode
  action: string
  onConfirm: () => Promise<void>
  onClose: () => void
}

export function ConfirmDialog({ title, sub, action, onConfirm, onClose }: ConfirmDialogProps) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const run = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await onConfirm()
      onClose()
    } catch (e) {
      setError(errorMessage(e))
      setBusy(false)
    }
  }

  return (
    <Modal
      confirm
      title={title}
      sub={sub}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn-outline" onClick={onClose} aria-disabled={busy || undefined}>
            Cancel
          </button>
          <button type="submit" className="btn btn-danger" aria-disabled={busy || undefined}>
            {action}
          </button>
        </>
      }
      onSubmit={run}
    >
      {error && <Notice detail={error} />}
    </Modal>
  )
}
