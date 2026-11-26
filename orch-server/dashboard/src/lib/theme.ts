import { useCallback, useState } from 'react';

export type ThemePreference = 'light' | 'dark' | 'system';

const STORAGE_KEY = 'haniel-theme';

function readPreference(): ThemePreference {
  const stored = localStorage.getItem(STORAGE_KEY);
  return stored === 'light' || stored === 'dark' ? stored : 'system';
}

// "system" removes data-theme so the prefers-color-scheme rule in index.css applies.
// index.html runs the same logic before first paint.
function applyPreference(preference: ThemePreference) {
  if (preference === 'system') {
    document.documentElement.removeAttribute('data-theme');
    localStorage.removeItem(STORAGE_KEY);
  } else {
    document.documentElement.setAttribute('data-theme', preference);
    localStorage.setItem(STORAGE_KEY, preference);
  }
}

export function useTheme(): [ThemePreference, (next: ThemePreference) => void] {
  const [preference, setPreference] = useState<ThemePreference>(readPreference);
  const choose = useCallback((next: ThemePreference) => {
    applyPreference(next);
    setPreference(next);
  }, []);
  return [preference, choose];
}
