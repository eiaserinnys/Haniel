import * as Dialog from '@radix-ui/react-dialog'
import type { ReactNode } from 'react'
import { useOpener } from '@/hooks/useOpener'
import { cx } from '@/lib/utils'

interface ModalProps {
  title: string
  sub?: ReactNode
  confirm?: boolean
  onClose: () => void
  onSubmit?: () => void
  footer: ReactNode
  children?: ReactNode
}

/** Dialog on desktop, bottom sheet on a phone-sized window (CSS). Focus trap, Esc and focus return come from Radix. */
export function Modal({ title, sub, confirm, onClose, onSubmit, footer, children }: ModalProps) {
  const returnFocus = useOpener()
  return (
    <Dialog.Root open onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="scrim" />
        <Dialog.Content
          className={cx('dialog', confirm && 'is-confirm')}
          role={confirm ? 'alertdialog' : 'dialog'}
          aria-describedby={undefined}
          onCloseAutoFocus={returnFocus}
        >
          <div className="dialog-head">
            <Dialog.Title className="dialog-title">{title}</Dialog.Title>
            {sub && <p className="dialog-sub">{sub}</p>}
          </div>
          <form
            className="contents"
            onSubmit={(e) => {
              e.preventDefault()
              onSubmit?.()
            }}
          >
            <div className="dialog-body scroll">{children}</div>
            <div className="dialog-foot">{footer}</div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
