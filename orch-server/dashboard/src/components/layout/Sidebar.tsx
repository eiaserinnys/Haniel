import type { Page } from '@/types';

interface NavProps {
  page: Page;
  setPage: (p: Page) => void;
  pendingCount: number;
  nodesConnected: number;
  historyCount: number;
}

export function Nav({ page, setPage, pendingCount, nodesConnected, historyCount }: NavProps) {
  const rows: [Page, string, number, boolean][] = [
    ['pending', 'Pending Deploys', pendingCount, true],
    ['nodes', 'Nodes', nodesConnected, false],
    ['history', 'Deploy History', historyCount, false],
  ];
  return (
    <nav className="nav" aria-label="Pages">
      {rows.map(([key, label, count, hot]) => (
        <button
          key={key}
          type="button"
          className="nav-row"
          aria-current={page === key ? 'page' : undefined}
          onClick={() => setPage(key)}
        >
          <span>{label}</span>
          <span className={hot && count > 0 ? 'nav-count is-hot' : 'nav-count'}>{count}</span>
        </button>
      ))}
    </nav>
  );
}

export function Sidebar(props: NavProps) {
  return (
    <aside className="sidebar">
      <Nav {...props} />
    </aside>
  );
}
