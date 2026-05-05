import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useWebSocket } from './useWebSocket';

// Mock WebSocket
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 0;
  close = vi.fn();

  constructor(public url: string) {
    MockWebSocket.instances.push(this);
  }

  simulateOpen() { this.readyState = 1; this.onopen?.(); }
  simulateClose() { this.readyState = 3; this.onclose?.(); }
  simulateMessage(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) }); }
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal('WebSocket', MockWebSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('useWebSocket', () => {
  it('connects to /ws/dashboard', () => {
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onEvent));
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(MockWebSocket.instances[0].url).toContain('/ws/dashboard');
  });

  it('sets status to connected on open', () => {
    const onEvent = vi.fn();
    const { result } = renderHook(() => useWebSocket(onEvent));
    act(() => { MockWebSocket.instances[0].simulateOpen(); });
    expect(result.current.status).toBe('connected');
  });

  it('dispatches parsed events', () => {
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onEvent));
    const ws = MockWebSocket.instances[0];
    act(() => { ws.simulateOpen(); });
    act(() => { ws.simulateMessage({ type: 'new_pending', deploy_id: 'd1', node_id: 'n1', repo: 'r', branch: 'b', detected_at: '2025-01-01' }); });
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ type: 'new_pending', deploy_id: 'd1' }));
  });

  it('reconnects with exponential backoff on close', () => {
    const onEvent = vi.fn();
    const { result } = renderHook(() => useWebSocket(onEvent));
    const ws = MockWebSocket.instances[0];
    act(() => { ws.simulateOpen(); });
    act(() => { ws.simulateClose(); });
    expect(result.current.status).toBe('reconnecting');

    // After 1s delay, should try to reconnect
    act(() => { vi.advanceTimersByTime(1000); });
    expect(MockWebSocket.instances).toHaveLength(2);
  });

  it('sets disconnected after max retries', () => {
    const onEvent = vi.fn();
    const { result } = renderHook(() => useWebSocket(onEvent));

    // Simulate 5 failed connections
    for (let i = 0; i < 6; i++) {
      const ws = MockWebSocket.instances[MockWebSocket.instances.length - 1];
      act(() => { ws.simulateClose(); });
      act(() => { vi.advanceTimersByTime(20000); }); // advance past any backoff
    }

    expect(result.current.status).toBe('disconnected');
  });

  it('resets retries on successful connection', () => {
    const onEvent = vi.fn();
    const { result } = renderHook(() => useWebSocket(onEvent));
    const ws1 = MockWebSocket.instances[0];

    // Close without connecting
    act(() => { ws1.simulateClose(); });
    act(() => { vi.advanceTimersByTime(1000); });

    // Second attempt succeeds
    const ws2 = MockWebSocket.instances[1];
    act(() => { ws2.simulateOpen(); });
    expect(result.current.status).toBe('connected');

    // Close again — should restart retries from 0
    act(() => { ws2.simulateClose(); });
    expect(result.current.status).toBe('reconnecting');
  });
});
