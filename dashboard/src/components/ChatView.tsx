import { useEffect, useRef } from "react";
import type { ChatMessage } from "@/hooks/useChatWebSocket";
import { MarkdownContent } from "./MarkdownContent";

interface ChatViewProps {
  messages: ChatMessage[];
}

function UserMessage({ msg }: { msg: ChatMessage }) {
  return (
    <div className="flex gap-2 px-3 py-1.5">
      <div className="flex flex-col gap-0.5 min-w-0">
        <span className="text-[15px] font-bold text-accent-blue uppercase tracking-wide">
          USER
        </span>
        <div className="text-[15px] text-foreground prose prose-sm prose-invert max-w-none break-words">
          <MarkdownContent content={msg.content} />
        </div>
      </div>
    </div>
  );
}

function AssistantMessage({ msg }: { msg: ChatMessage }) {
  return (
    <div className="flex gap-2 px-3 py-1.5">
      <div className="flex flex-col gap-0.5 min-w-0">
        <span className="text-[15px] font-bold text-accent-green uppercase tracking-wide">
          CLAUDE
        </span>
        {msg.isStreaming ? (
          <span className="text-[15px] text-foreground whitespace-pre-wrap break-words">
            {msg.content}
            <span className="inline-block w-[2px] h-[1em] bg-current animate-pulse align-middle ml-0.5" />
          </span>
        ) : (
          <div className="text-[15px] text-foreground prose prose-sm prose-invert max-w-none break-words">
            <MarkdownContent content={msg.content} />
          </div>
        )}
      </div>
    </div>
  );
}

function SystemMessage({ msg }: { msg: ChatMessage }) {
  return (
    <div className="px-3 py-1 text-[12px] text-muted-foreground italic">
      {msg.content}
    </div>
  );
}

function MessageItem({ msg }: { msg: ChatMessage }) {
  if (msg.role === "user") return <UserMessage msg={msg} />;
  if (msg.role === "assistant") return <AssistantMessage msg={msg} />;
  return <SystemMessage msg={msg} />;
}

export function ChatView({ messages }: ChatViewProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="flex flex-col">
        {messages.map((msg) => (
          <MessageItem key={msg.id} msg={msg} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
