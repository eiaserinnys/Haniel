import { useState } from "react";
import { useChatWebSocket } from "@/hooks/useChatWebSocket";
import { ChatView } from "./ChatView";
import { ChatInput } from "./ChatInput";
import { SessionListModal } from "./SessionListModal";

export function ChatPanel() {
  const chat = useChatWebSocket();
  const [sessionListOpen, setSessionListOpen] = useState(false);

  return (
    <div className="flex flex-col h-full bg-background text-foreground">
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border shrink-0">
        <span className="text-sm font-medium text-foreground flex-1">Claude</span>
        <button
          className="text-xs text-muted-foreground hover:text-foreground px-2 py-1 rounded hover:bg-muted transition-colors"
          onClick={() => {
            chat.loadSessions();
            setSessionListOpen(true);
          }}
        >
          Sessions
        </button>
        <button
          className="text-xs text-muted-foreground hover:text-foreground px-2 py-1 rounded hover:bg-muted transition-colors"
          onClick={() => chat.startNewSession()}
        >
          New Session
        </button>
      </div>

      <ChatView messages={chat.messages} />
      <ChatInput onSend={chat.sendMessage} disabled={!chat.connected} />

      <SessionListModal
        open={sessionListOpen}
        sessions={chat.sessions}
        onSelect={chat.switchSession}
        onClose={() => setSessionListOpen(false)}
      />
    </div>
  );
}
