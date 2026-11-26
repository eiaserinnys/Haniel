import { Dialog } from '@base-ui/react/dialog';
import { Icon } from '@/components/shared/Icon';
import { Nav } from '@/components/layout/Sidebar';
import type { Page } from '@/types';

interface DrawerProps {
  open: boolean;
  onClose: () => void;
  page: Page;
  setPage: (p: Page) => void;
  pendingCount: number;
  nodesConnected: number;
  historyCount: number;
}

export function Drawer({ open, onClose, ...nav }: DrawerProps) {
  return (
    <Dialog.Root open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="scrim is-drawer" />
        <Dialog.Popup className="drawer" aria-label="Menu">
          <div className="drawer-head">
            <div className="brand">
              <span className="brand-name">Haniel</span>
              <span className="brand-sub">Orchestrator</span>
            </div>
            <Dialog.Close className="icon-btn" aria-label="Close menu">
              <Icon name="x" />
            </Dialog.Close>
          </div>
          <Nav {...nav} />
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
