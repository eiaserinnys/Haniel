import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import {
  useInFlightCommands,
  SERVICE_SPINNER_MIN_MS,
} from './useInFlightCommands';

describe('useInFlightCommands', () => {
  const sample = (cid: string, n = 'n1', s = 'bot') => ({
    commandId: cid,
    nodeId: n,
    serviceName: s,
    action: 'restart' as const,
  });

  it('add inserts a command keyed by commandId', () => {
    const { result } = renderHook(() => useInFlightCommands());
    act(() => result.current.add(sample('cid-1')));
    expect(result.current.inFlight.size).toBe(1);
    expect(result.current.inFlight.get('cid-1')?.serviceName).toBe('bot');
  });

  it('add fills addedAt automatically with Date.now()', () => {
    const { result } = renderHook(() => useInFlightCommands());
    const before = Date.now();
    act(() => result.current.add(sample('cid-now')));
    const after = Date.now();
    const stored = result.current.inFlight.get('cid-now');
    expect(stored).toBeDefined();
    expect(typeof stored!.addedAt).toBe('number');
    expect(stored!.addedAt).toBeGreaterThanOrEqual(before);
    expect(stored!.addedAt).toBeLessThanOrEqual(after);
  });

  it('remove deletes by commandId; no-op for unknown id', () => {
    const { result } = renderHook(() => useInFlightCommands());
    act(() => result.current.add(sample('cid-1')));
    act(() => result.current.remove('cid-1'));
    expect(result.current.inFlight.size).toBe(0);
    act(() => result.current.remove('unknown'));
    expect(result.current.inFlight.size).toBe(0);
  });

  it('clear empties all entries', () => {
    const { result } = renderHook(() => useInFlightCommands());
    act(() => {
      result.current.add(sample('a', 'n1', 's1'));
      result.current.add(sample('b', 'n2', 's2'));
    });
    act(() => result.current.clear());
    expect(result.current.inFlight.size).toBe(0);
  });

  it('lookupByService returns matching command or null', () => {
    const { result } = renderHook(() => useInFlightCommands());
    act(() => result.current.add(sample('a', 'n1', 'bot')));
    expect(result.current.lookupByService('n1', 'bot')?.commandId).toBe('a');
    expect(result.current.lookupByService('n1', 'other')).toBeNull();
    expect(result.current.lookupByService('other', 'bot')).toBeNull();
  });

  describe('removeWithMinDelay', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it('removes immediately when elapsed >= minMs', () => {
      const { result } = renderHook(() => useInFlightCommands());
      act(() => result.current.add(sample('cid-fast')));
      // Advance past the minimum so removeWithMinDelay sees elapsed >= minMs.
      act(() => vi.advanceTimersByTime(600));
      act(() => result.current.removeWithMinDelay('cid-fast', 500));
      expect(result.current.inFlight.size).toBe(0);
    });

    it('defers removal until min elapsed', () => {
      const { result } = renderHook(() => useInFlightCommands());
      act(() => result.current.add(sample('cid-slow')));
      // Immediate request — should defer.
      act(() => result.current.removeWithMinDelay('cid-slow', 500));
      expect(result.current.inFlight.size).toBe(1);

      // 200ms passed; still not enough.
      act(() => vi.advanceTimersByTime(200));
      expect(result.current.inFlight.size).toBe(1);

      // Total 700ms ≥ 500ms — removal fires.
      act(() => vi.advanceTimersByTime(500));
      expect(result.current.inFlight.size).toBe(0);
    });

    it('is a no-op for unknown id', () => {
      const { result } = renderHook(() => useInFlightCommands());
      act(() => result.current.add(sample('cid-other')));
      // Should not throw and should not affect state.
      act(() => result.current.removeWithMinDelay('unknown', 500));
      expect(result.current.inFlight.size).toBe(1);
    });

    it('clear cancels pending defer timers', () => {
      const { result } = renderHook(() => useInFlightCommands());
      act(() => result.current.add(sample('cid-cleared')));
      act(() => result.current.removeWithMinDelay('cid-cleared', 500));
      expect(result.current.inFlight.size).toBe(1);

      // clear should drop the entry now AND cancel the deferred removal.
      act(() => result.current.clear());
      expect(result.current.inFlight.size).toBe(0);

      // After the timer would have fired, no spurious mutation.
      act(() => vi.advanceTimersByTime(2000));
      expect(result.current.inFlight.size).toBe(0);
    });

    it('SERVICE_SPINNER_MIN_MS is exported as a number', () => {
      expect(typeof SERVICE_SPINNER_MIN_MS).toBe('number');
      expect(SERVICE_SPINNER_MIN_MS).toBeGreaterThan(0);
    });
  });
});
