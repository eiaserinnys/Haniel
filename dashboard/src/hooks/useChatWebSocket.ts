import { useEffect, useRef, useCallback, useState } from "react";

// ── Types (local — no dependency on src/lib/) ────────────────────────────────

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  isStreaming?: boolean;
}

export interface ChatSession {
  id: string;
  created_at: string;
  last_active_at: string;
  preview?: string | null;
}

// ── Reconnect constants (mirrors useWebSocket.ts) ─────────────────────────────

const INITIAL_DELAY = 1000;
const MAX_DELAY = 30000;
const BACKOFF_FACTOR = 2;

// ── Hook ──────────────────────────────────────────────────────────────────────

export interface UseChatWebSocket {
  messages: ChatMessage[];
  sessions: ChatSession[];
  activeSessionId: string | null;
  connected: boolean;
  sendMessage: (text: string) => void;
  loadSessions: () => void;
  loadHistory: (sessionId: string) => void;
  startNewSession: () => void;
  switchSession: (sessionId: string) => void;
}

export function useChatWebSocket(): UseChatWebSocket {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const retryDelayRef = useRef(INITIAL_DELAY);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const connectionIdRef = useRef(0);
  const handleServerMessageRef = useRef<(raw: string) => void>(() => {});

  const sendRaw = useCallback((data: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  const loadHistory = useCallback(
    (sessionId: string) => {
      sendRaw({ type: "load_history", session_id: sessionId });
    },
    [sendRaw]
  );

  const handleServerMessage = useCallback((raw: string) => {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      return;
    }

    const type = msg.type as string;

    if (type === "sessions_list") {
      const list = (msg.sessions ?? []) as ChatSession[];
      setSessions(list);
      // Auto-activate the most recently used session and load its history
      if (list.length > 0) {
        const sorted = [...list].sort(
          (a, b) =>
            new Date(b.last_active_at).getTime() -
            new Date(a.last_active_at).getTime()
        );
        const mostRecentId = sorted[0].id;
        setActiveSessionId(mostRecentId);
        sendRaw({ type: "load_history", session_id: mostRecentId });
      }
    } else if (type === "history") {
      type HistoryEntry = { role: string; content: string; ts: string };
      const historyMsgs = (msg.messages ?? []) as HistoryEntry[];
      const chatMsgs: ChatMessage[] = historyMsgs
        .filter((m) => m.role === "user" || m.role === "assistant")
        .map((m) => ({
          id: crypto.randomUUID(),
          role: m.role as "user" | "assistant",
          content: m.content,
        }));
      setMessages(chatMsgs);
    } else if (type === "session_start") {
      const sessionId = msg.session_id as string;
      const isNew = msg.is_new as boolean;
      const resumed = msg.resumed as boolean;
      setActiveSessionId(sessionId);

      // Session connection notification
      if (resumed) {
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "system",
            content: `Reconnected to session ${sessionId.slice(0, 8)}…`,
          },
        ]);
      } else if (!isNew) {
        // Existing session but resume failed → replaced with new session
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "system",
            content: "Could not resume previous session. A new session has been created.",
          },
        ]);
      }
    } else if (type === "text_delta") {
      const delta = (msg.delta as string) ?? "";
      setMessages((prev) => {
        if (prev.length === 0) return prev;
        const last = prev[prev.length - 1];
        if (last.role === "assistant") {
          return [
            ...prev.slice(0, -1),
            { ...last, content: last.content + delta, isStreaming: true },
          ];
        }
        // First delta — append new assistant message
        return [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: "assistant",
            content: delta,
            isStreaming: true,
          },
        ];
      });
    } else if (type === "message_end") {
      setMessages((prev) => {
        if (prev.length === 0) return prev;
        const last = prev[prev.length - 1];
        if (last.role === "assistant") {
          return [...prev.slice(0, -1), { ...last, isStreaming: false }];
        }
        return prev;
      });
    } else if (type === "compact_start") {
      const retry = msg.retry as number;
      const maxRetries = msg.max_retries as number;
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: `Context compaction in progress... (${retry}/${maxRetries})`,
        },
      ]);
    } else if (type === "compact_end") {
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: "Compaction complete, resuming response.",
        },
      ]);
    } else if (type === "error") {
      const error = (msg.error as string) ?? "unknown error";
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: "system",
          content: `⚠ ${error}`,
        },
      ]);
    }
  }, [sendRaw]);

  // Keep ref in sync so connect's stable closure always calls the latest version
  useEffect(() => {
    handleServerMessageRef.current = handleServerMessage;
  });

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    const myId = ++connectionIdRef.current;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}/ws/chat`;
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current || myId !== connectionIdRef.current) { ws.close(); return; }
      setConnected(true);
      retryDelayRef.current = INITIAL_DELAY;
      // Auto-load sessions on connect
      ws.send(JSON.stringify({ type: "list_sessions" }));
    };

    ws.onmessage = (ev) => {
      if (!mountedRef.current || myId !== connectionIdRef.current) return;
      handleServerMessageRef.current(ev.data as string);
    };

    ws.onerror = () => { /* onerror always precedes onclose */ };

    ws.onclose = () => {
      if (!mountedRef.current || myId !== connectionIdRef.current) return;
      wsRef.current = null;
      setConnected(false);

      const delay = retryDelayRef.current;
      retryDelayRef.current = Math.min(delay * BACKOFF_FACTOR, MAX_DELAY);
      retryTimerRef.current = setTimeout(() => {
        if (mountedRef.current) connect();
      }, delay);
    };
  }, []); // stable — no deps needed; uses refs for latest values

  useEffect(() => {
    mountedRef.current = true;
    connect();
    return () => {
      mountedRef.current = false;
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
      wsRef.current?.close();
    };
  }, []); // connect is stable, so [] is correct

  const sendMessage = useCallback(
    (text: string) => {
      if (!text.trim()) return;
      setMessages((prev) => [
        ...prev,
        { id: crypto.randomUUID(), role: "user", content: text },
      ]);
      sendRaw({ type: "send_message", session_id: activeSessionId, text });
    },
    [activeSessionId, sendRaw]
  );

  const loadSessions = useCallback(() => {
    sendRaw({ type: "list_sessions" });
  }, [sendRaw]);

  const startNewSession = useCallback(() => {
    setMessages([]);
    sendRaw({ type: "new_session" });
  }, [sendRaw]);

  const switchSession = useCallback(
    (sessionId: string) => {
      setMessages([]);
      sendRaw({ type: "send_message", session_id: sessionId, text: "" });
    },
    [sendRaw]
  );

  return {
    messages,
    sessions,
    activeSessionId,
    connected,
    sendMessage,
    loadSessions,
    loadHistory,
    startNewSession,
    switchSession,
  };
}
