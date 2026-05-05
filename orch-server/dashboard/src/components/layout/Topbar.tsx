import { Icon } from '@/components/shared/Icon';
import type { Page } from '@/types';

interface TopbarProps {
  page: Page;
  pendingCount: number;
  theme: 'dark' | 'light';
  onToggleTheme: () => void;
  onMenuToggle: () => void;
}

const PAGE_LABELS: Record<Page, string> = {
  pending: 'Pending Deploys',
  nodes: 'Nodes',
  history: 'Deploy History',
};

export function Topbar({ page, pendingCount, theme, onToggleTheme, onMenuToggle }: TopbarProps) {
  return (
    <header className="topbar">
      <button className="top-action menu-btn" aria-label="Menu" onClick={onMenuToggle}>
        <Icon name="menu" size={14} />
      </button>
      <div className="crumbs">
        <b style={{ color: 'var(--fg)' }}>Orchestrator Dashboard</b>
        <span style={{ margin: '0 8px', color: 'var(--dim)' }}>/</span>
        <span>{PAGE_LABELS[page]}</span>
      </div>
      <div className="spacer" />
      <button
        className="top-action"
        aria-label="Toggle theme"
        title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        onClick={onToggleTheme}
      >
        <Icon name={theme === 'dark' ? 'sun' : 'moon'} size={14} />
      </button>
      <button className="top-action" title={`${pendingCount} pending`}>
        <Icon name="bell" size={14} />
        {pendingCount > 0 && <span className="bell-badge" />}
      </button>
    </header>
  );
}
