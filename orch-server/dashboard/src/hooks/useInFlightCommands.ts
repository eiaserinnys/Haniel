import { useState, useCallback, useRef, useEffect } from 'react';
import type { InFlightCommand } from '@/types';

/**
 * Minimum visible duration for the service-command spinner. Even when the
 * node responds in <100 ms, the button stays in its in-flight state for
 * SERVICE_SPINNER_MIN_MS so the user perceives that something happened.
 *
 * UX policy — not user-tunable. No env override.
 */
export const SERVICE_SPINNER_MIN_MS = 500;

/**
 * Single source of truth for in-flight service commands triggered from the
 * dashboard. Keyed by `command_id` (server-generated, unique per request).
 *
 * `lookupByService` exposes a derived view by `(nodeId, serviceName)` so
 * that the NodesView can disable its Restart/Stop buttons while a command
 * is outstanding.
 *
 * `removeWithMinDelay` is the canonical removal path for the
 * service_command_result handler — it guarantees the spinner is visible
 * for at least `minMs` (typically `SERVICE_SPINNER_MIN_MS`).
 */
export function useInFlightCommands() {
  const [inFlight, setInFlight] = useState<Map<string, InFlightCommand>>(
    () => new Map(),
  );

  // Ref mirror so removeWithMinDelay reads the latest state without
  // including `inFlight` in its useCallback deps. This keeps the callback
  // identity stable across re-renders, so dependent hooks (e.g. handleWsEvent)
  // don't re-create themselves on every state change.
  const inFlightRef = useRef(inFlight);
  inFlightRef.current = inFlight;

  // Active deferred-removal timers, keyed by commandId. Tracked so we can
  // cancel them on clear() / unmount and so removeWithMinDelay can avoid
  // double-scheduling for the same commandId.
  const pendingTimers = useRef<Map<string, number>>(new Map());

  const add = useCallback(
    (cmd: Omit<InFlightCommand, 'addedAt'> & { addedAt?: number }) => {
      setInFlight((prev) => {
        const next = new Map(prev);
        next.set(cmd.commandId, { ...cmd, addedAt: cmd.addedAt ?? Date.now() });
        return next;
      });
    },
    [],
  );

  const remove = useCallback((commandId: string) => {
    setInFlight((prev) => {
      if (!prev.has(commandId)) return prev;
      const next = new Map(prev);
      next.delete(commandId);
      return next;
    });
  }, []);

  // Schedule removal so that the spinner is shown for at least `minMs`
  // since the command was added. setTimeout is registered OUTSIDE the
  // setState updater, which keeps the side effect from being duplicated
  // when React strict mode invokes the updater twice.
  const removeWithMinDelay = useCallback(
    (commandId: string, minMs: number) => {
      const cmd = inFlightRef.current.get(commandId);
      if (!cmd) return; // unknown id — no-op (mirrors remove)
      const elapsed = Date.now() - cmd.addedAt;
      if (elapsed >= minMs) {
        setInFlight((prev) => {
          if (!prev.has(commandId)) return prev;
          const next = new Map(prev);
          next.delete(commandId);
          return next;
        });
        return;
      }
      if (pendingTimers.current.has(commandId)) return; // double-schedule guard
      const remaining = minMs - elapsed;
      const timerId = window.setTimeout(() => {
        pendingTimers.current.delete(commandId);
        setInFlight((p) => {
          if (!p.has(commandId)) return p;
          const n = new Map(p);
          n.delete(commandId);
          return n;
        });
      }, remaining);
      pendingTimers.current.set(commandId, timerId);
    },
    [],
  );

  const clear = useCallback(() => {
    for (const tid of pendingTimers.current.values()) window.clearTimeout(tid);
    pendingTimers.current.clear();
    setInFlight((prev) => (prev.size === 0 ? prev : new Map()));
  }, []);

  // Unmount safety — clear any outstanding deferred-removal timers.
  useEffect(
    () => () => {
      for (const tid of pendingTimers.current.values()) window.clearTimeout(tid);
      pendingTimers.current.clear();
    },
    [],
  );

  // Derived lookup for UI: returns the in-flight command for a given service,
  // or null if no command is outstanding.
  const lookupByService = useCallback(
    (nodeId: string, serviceName: string): InFlightCommand | null => {
      for (const cmd of inFlight.values()) {
        if (cmd.nodeId === nodeId && cmd.serviceName === serviceName) {
          return cmd;
        }
      }
      return null;
    },
    [inFlight],
  );

  return {
    inFlight,
    add,
    remove,
    removeWithMinDelay,
    clear,
    lookupByService,
  };
}
