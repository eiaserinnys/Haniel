import { Menu } from '@base-ui/react/menu';
import { Icon, type IconName } from '@/components/shared/Icon';
import type { ThemePreference } from '@/lib/theme';

interface ThemeMenuProps {
  theme: ThemePreference;
  onChange: (theme: ThemePreference) => void;
}

const OPTIONS: { value: ThemePreference; label: string; icon: IconName }[] = [
  { value: 'light', label: 'Light', icon: 'sun' },
  { value: 'dark', label: 'Dark', icon: 'moon' },
  { value: 'system', label: 'System', icon: 'system' },
];

export function ThemeMenu({ theme, onChange }: ThemeMenuProps) {
  const current = OPTIONS.find(o => o.value === theme) ?? OPTIONS[2];
  return (
    <Menu.Root>
      <Menu.Trigger className="icon-btn" aria-label={`Theme: ${current.label}`}>
        <Icon name={current.icon} />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner className="menu-pos" align="end" sideOffset={4}>
          <Menu.Popup className="menu" aria-label="Theme">
            <Menu.RadioGroup value={theme} onValueChange={(v) => onChange(v as ThemePreference)}>
              {OPTIONS.map(o => (
                <Menu.RadioItem key={o.value} value={o.value} className="menu-item" closeOnClick>
                  <Icon name={o.icon} />
                  <span>{o.label}</span>
                  <Menu.RadioItemIndicator keepMounted={false} render={<span className="tick" />}>
                    <Icon name="check" />
                  </Menu.RadioItemIndicator>
                </Menu.RadioItem>
              ))}
            </Menu.RadioGroup>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}
