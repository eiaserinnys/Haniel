import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Toaster } from './Toaster';
import type { Toast, ToastKind } from '@/types';

const TONES: Record<ToastKind, string> = {
  success: 'st-ok',
  busy: 'st-busy',
  warn: 'st-wait',
  neutral: 'st-off',
  error: 'st-fail',
};

describe('Toaster', () => {
  it('draws the marker for the toast state, filled green only for success', () => {
    const toasts: Toast[] = (Object.keys(TONES) as ToastKind[]).map(kind => ({ id: kind, text: kind, kind }));
    const { container } = render(<Toaster toasts={toasts} onDismiss={() => undefined} />);
    for (const kind of Object.keys(TONES) as ToastKind[]) {
      const chip = Array.from(container.querySelectorAll('.toast .st')).find(el => el.textContent === kind);
      expect(chip?.classList.contains(TONES[kind])).toBe(true);
    }
  });
});
