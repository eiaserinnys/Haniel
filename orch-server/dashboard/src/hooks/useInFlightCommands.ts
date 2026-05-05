import { useState, useCallback } from 'react';
import type { InFlightCommand } from '@/types';

/**
 * Single source of truth for in-flight service commands triggered from the
 * dashboard. Keyed by `command_id` (server-generated, unique per request).
 *
 * `lookupByService` exposes a derived view by `(nodeId, serviceName)` so
 * that the NodesView can disable its Restart/Stop buttons while a command
 * is outstanding.
 */
export function useInFlightCommands() {
  const [inFlight, setInFlight] = useState<Map<string, InFlightCommand>>(
    () => new Map(),
  );

  const add = useCallback((cmd: InFlightCommand) => {
    setInFlight((prev) => {
      const next = new Map(prev);
      next.set(cmd.commandId, cmd);
      return next;
    });
  }, []);

  const remove = useCallback((commandId: string) => {
    setInFlight((prev) => {
      if (!prev.has(commandId)) return prev;
      const next = new Map(prev);
      next.delete(commandId);
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    setInFlight((prev) => (prev.size === 0 ? prev : new Map()));
  }, []);

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

  return { inFlight, add, remove, clear, lookupByService };
}
