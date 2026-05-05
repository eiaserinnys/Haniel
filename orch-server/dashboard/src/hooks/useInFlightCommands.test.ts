import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useInFlightCommands } from './useInFlightCommands';

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
});
