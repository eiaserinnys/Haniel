import { Icon, type IconName } from '@/components/shared/Icon';
import type { Page } from '@/types';

interface MobileNavProps {
  page: Page;
  setPage: (p: Page) => void;
  pendingCount: number;
}

const TABS: [Page, string, IconName][] = [
  ['pending', 'Pending', 'inbox'],
  ['nodes', 'Nodes', 'server'],
  ['history', 'History', 'history'],
];

export function MobileNav({ page, setPage, pendingCount }: MobileNavProps) {
  return (
    <nav className="tabbar" aria-label="Pages">
      {TABS.map(([key, label, icon]) => (
        <button
          key={key}
          type="button"
          className="tab-item"
          aria-current={page === key ? 'page' : undefined}
          onClick={() => setPage(key)}
        >
          <Icon name={icon} />
          <span className="lbl">
            {label}
            {key === 'pending' && pendingCount > 0 && <span className="nav-count is-hot">{pendingCount}</span>}
          </span>
        </button>
      ))}
    </nav>
  );
}
