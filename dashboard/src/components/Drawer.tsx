import * as Dialog from '@radix-ui/react-dialog'
import type { ReactNode } from 'react'
import { Icon } from './Icon'
import { useOpener } from '@/hooks/useOpener'

/** Phone-sized replacement for the sidebar. Radix gives the focus trap, Esc, and focus return to the menu button. */
export function Drawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const returnFocus = useOpener()
  return (
    <Dialog.Root open onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="scrim is-drawer" />
        <Dialog.Content className="drawer" aria-describedby={undefined} onCloseAutoFocus={returnFocus}>
          <div className="drawer-head">
            <div className="brand">
              <span className="brand-name">Haniel</span>
              <span className="brand-sub">Local</span>
            </div>
            <Dialog.Title className="sr-only">Menu</Dialog.Title>
            <Dialog.Close className="icon-btn" aria-label="Close menu">
              <Icon name="x" />
            </Dialog.Close>
          </div>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
