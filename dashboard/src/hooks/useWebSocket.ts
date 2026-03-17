import { useEffect, useRef, useCallback, useState } from "react";
import type { WsEvent } from "@/lib/types";

type ConnectionState = "connecting" | "connected" | "reconnecting" | "disconnected";

interface UseWebSocketOptions {
  onEvent: (event: WsEvent) => void;
  url?: string;
}

const INITIAL_DELAY = 1000;
const MAX_DELAY = 30000;
const BACKOFF_FACTOR = 2;

export function useWebSocket({ onEvent, url = "/ws" }: UseWebSocketOptions) {
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const wsRef = useRef<WebSocket | null>(null);
  const retryDelayRef = useRef(INITIAL_DELAY);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const connect = useCallback(() => {
    if (!mountedRef.current) return;

    const wsUrl = `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}${url}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) { ws.close(); return; }
      setConnectionState("connected");
      retryDelayRef.current = INITIAL_DELAY;
    };

    ws.onmessage = (ev) => {
      if (!mountedRef.current) return;
      try {
        const event = JSON.parse(ev.data as string) as WsEvent;
        onEventRef.current(event);
      } catch {
        // ignore malformed messages
      }
    };

    ws.onerror = () => {
      // onerror always precedes onclose
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      wsRef.current = null;
      setConnectionState("reconnecting");

      const delay = retryDelayRef.current;
      retryDelayRef.current = Math.min(delay * BACKOFF_FACTOR, MAX_DELAY);

      retryTimerRef.current = setTimeout(() => {
        if (mountedRef.current) connect();
      }, delay);
    };
  }, [url]);

  useEffect(() => {
    mountedRef.current = true;
    setConnectionState("connecting");
    connect();

    return () => {
      mountedRef.current = false;
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { connectionState };
}
