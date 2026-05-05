import { Icon } from '@/components/shared/Icon';
import type { Page, WsStatus } from '@/types';
import { cn } from '@/lib/utils';

interface SidebarProps {
  page: Page;
  setPage: (p: Page) => void;
  pendingCount: number;
  nodesConnected: number;
  historyCount: number;
  wsStatus: WsStatus;
}

export function Sidebar({ page, setPage, pendingCount, nodesConnected, historyCount, wsStatus }: SidebarProps) {
  return (
    <aside className="sidebar">
      {/* Brand */}
      <div className="brand">
        <img className="brand-mark" src="/dashboard/haniel-icon.png" alt="Haniel" />
        <div>
          <div className="brand-name">Haniel</div>
          <div className="brand-sub">Orchestrator</div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="nav">
        <div className="nav-section">Dashboard</div>
        <NavBtn icon="inbox" label="Pending Deploys" active={page === 'pending'}
                onClick={() => setPage('pending')} count={pendingCount} pending />
        <NavBtn icon="server" label="Nodes" active={page === 'nodes'}
                onClick={() => setPage('nodes')} count={nodesConnected} />
        <NavBtn icon="history" label="Deploy History" active={page === 'history'}
                onClick={() => setPage('history')} count={historyCount} />
      </nav>

      {/* Footer */}
      <div className="sidebar-ft">
        <div className="ws-status">
          <span className={cn('ws-led', wsStatus === 'connected' && 'is-on', wsStatus === 'reconnecting' && 'is-reconnecting')} />
          <span>
            {wsStatus === 'connected' ? 'WebSocket connected' :
             wsStatus === 'reconnecting' ? 'Reconnecting...' :
             'Disconnected'}
          </span>
        </div>
        <div className="ws-path">/ws/dashboard</div>
        <div className="version">haniel orchestrator</div>
      </div>
    </aside>
  );
}

interface NavBtnProps {
  icon: string;
  label: string;
  active: boolean;
  onClick: () => void;
  count?: number;
  pending?: boolean;
}

function NavBtn({ icon, label, active, onClick, count, pending }: NavBtnProps) {
  return (
    <button className={cn('nav-item', active && 'is-on')} onClick={onClick}>
      <Icon name={icon} size={14} style={{ color: active ? 'var(--fg)' : 'var(--muted)' }} />
      <span>{label}</span>
      {typeof count === 'number' && (
        <span className={cn('nav-count', pending && count > 0 && 'is-pending')}>{count}</span>
      )}
    </button>
  );
}
