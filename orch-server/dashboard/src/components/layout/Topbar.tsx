import { forwardRef } from 'react';
import { Icon } from '@/components/shared/Icon';
import { Status } from '@/components/shared/Status';
import { ThemeMenu } from '@/components/layout/ThemeMenu';
import type { ThemePreference } from '@/lib/theme';
import type { WsStatus } from '@/types';

interface TopbarProps {
  wsStatus: WsStatus;
  theme: ThemePreference;
  onThemeChange: (theme: ThemePreference) => void;
  menuLabel: string;
  onMenuToggle: () => void;
}

export const Topbar = forwardRef<HTMLButtonElement, TopbarProps>(function Topbar(
  { wsStatus, theme, onThemeChange, menuLabel, onMenuToggle },
  menuRef,
) {
  return (
    <header className="topbar">
      <button ref={menuRef} type="button" className="icon-btn" aria-label={menuLabel} onClick={onMenuToggle}>
        <Icon name="menu" />
      </button>
      <div className="brand">
        <span className="brand-name">Haniel</span>
        <span className="brand-sub">Orchestrator</span>
      </div>
      <div className="topbar-end">
        <span className="conn" role="status"><Status state={wsStatus} /></span>
        <ThemeMenu theme={theme} onChange={onThemeChange} />
      </div>
    </header>
  );
});
