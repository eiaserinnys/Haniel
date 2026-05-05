import { Icon } from '@/components/shared/Icon';
import type { Page } from '@/types';
import { cn } from '@/lib/utils';

interface MobileNavProps {
  page: Page;
  setPage: (p: Page) => void;
  pendingCount: number;
}

export function MobileNav({ page, setPage, pendingCount }: MobileNavProps) {
  return (
    <nav className="mobile-bottom-nav">
      <MobileTab icon="inbox" label="Pending" active={page === 'pending'}
                 onClick={() => setPage('pending')} count={pendingCount} pending />
      <MobileTab icon="server" label="Nodes" active={page === 'nodes'}
                 onClick={() => setPage('nodes')} />
      <MobileTab icon="history" label="History" active={page === 'history'}
                 onClick={() => setPage('history')} />
    </nav>
  );
}

interface MobileTabProps {
  icon: string;
  label: string;
  active: boolean;
  onClick: () => void;
  count?: number;
  pending?: boolean;
}

function MobileTab({ icon, label, active, onClick, count, pending }: MobileTabProps) {
  return (
    <button className={cn(active && 'is-on')} onClick={onClick} aria-label={label} title={label}>
      <Icon name={icon} size={20} />
      {pending && count != null && count > 0 && <span className="nav-count is-pending">{count}</span>}
    </button>
  );
}
