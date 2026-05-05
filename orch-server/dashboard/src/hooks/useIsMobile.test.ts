import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useIsMobile } from './useIsMobile';

let listeners: Array<(ev: { matches: boolean }) => void> = [];
let currentMatches = false;

const mockMatchMedia = vi.fn((query: string) => ({
  matches: currentMatches,
  media: query,
  addEventListener: (_: string, cb: (ev: { matches: boolean }) => void) => { listeners.push(cb); },
  removeEventListener: (_: string, cb: (ev: { matches: boolean }) => void) => {
    listeners = listeners.filter(l => l !== cb);
  },
}));

beforeEach(() => {
  listeners = [];
  currentMatches = false;
  vi.stubGlobal('matchMedia', mockMatchMedia);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useIsMobile', () => {
  it('returns false when viewport exceeds breakpoint', () => {
    currentMatches = false;
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);
  });

  it('returns true when viewport is within breakpoint', () => {
    currentMatches = true;
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(true);
  });

  it('uses default breakpoint of 760px', () => {
    renderHook(() => useIsMobile());
    expect(mockMatchMedia).toHaveBeenCalledWith('(max-width: 760px)');
  });

  it('accepts custom breakpoint', () => {
    renderHook(() => useIsMobile(1024));
    expect(mockMatchMedia).toHaveBeenCalledWith('(max-width: 1024px)');
  });

  it('updates when media query changes', () => {
    currentMatches = false;
    const { result } = renderHook(() => useIsMobile());
    expect(result.current).toBe(false);

    // Simulate viewport shrink
    act(() => {
      for (const listener of listeners) {
        listener({ matches: true });
      }
    });
    expect(result.current).toBe(true);

    // Simulate viewport expand
    act(() => {
      for (const listener of listeners) {
        listener({ matches: false });
      }
    });
    expect(result.current).toBe(false);
  });

  it('removes listener on unmount', () => {
    const { unmount } = renderHook(() => useIsMobile());
    expect(listeners.length).toBe(1);
    unmount();
    expect(listeners.length).toBe(0);
  });
});
