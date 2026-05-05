import { useEffect, useRef, useState, useCallback } from 'react';
import type { WsStatus, WsEvent } from '@/types';

const MAX_RETRIES = 5;
const INITIAL_DELAY = 1000;
const MAX_DELAY = 15000;

/**
 * WebSocket connection hook with exponential backoff reconnection.
 * URL: ws(s)://same-host/ws/dashboard
 */
export function useWebSocket(onEvent: (event: WsEvent) => void): {
  status: WsStatus;
  reconnect: () => void;
} {
  const [status, setStatus] = useState<WsStatus>('disconnected');
  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const connect = useCallback(() => {
    // Clean up existing connection
    if (wsRef.current) {
      wsRef.current.onopen = null;
      wsRef.current.onclose = null;
      wsRef.current.onmessage = null;
      wsRef.current.onerror = null;
      wsRef.current.close();
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = localStorage.getItem('haniel-token') || '';
    const qs = token ? `?token=${encodeURIComponent(token)}` : '';
    const url = `${protocol}//${window.location.host}/ws/dashboard${qs}`;

    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;
      setStatus('reconnecting');

      ws.onopen = () => {
        setStatus('connected');
        retriesRef.current = 0;
      };

      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as WsEvent;
          onEventRef.current(data);
        } catch {
          // Ignore unparseable messages
        }
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (retriesRef.current < MAX_RETRIES) {
          setStatus('reconnecting');
          const delay = Math.min(INITIAL_DELAY * Math.pow(2, retriesRef.current), MAX_DELAY);
          retriesRef.current++;
          timeoutRef.current = setTimeout(connect, delay);
        } else {
          setStatus('disconnected');
        }
      };

      ws.onerror = () => {
        // onclose will be called after onerror
      };
    } catch {
      setStatus('disconnected');
    }
  }, []);

  const reconnect = useCallback(() => {
    retriesRef.current = 0;
    connect();
  }, [connect]);

  useEffect(() => {
    connect();
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null; // prevent reconnect on intentional close
        wsRef.current.close();
      }
    };
  }, [connect]);

  return { status, reconnect };
}
