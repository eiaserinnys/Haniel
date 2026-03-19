import { useRef, type KeyboardEvent } from "react";

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
}

export function ChatInput({ onSend, disabled = false }: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSend = () => {
    const ta = textareaRef.current;
    if (!ta) return;
    const text = ta.value.trim();
    if (!text) return;
    onSend(text);
    ta.value = "";
    ta.style.height = "2rem"; // reset to h-8
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && e.ctrlKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleChange = () => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${ta.scrollHeight}px`;
  };

  return (
    <div className="border-t border-border p-3 shrink-0">
      <div className="flex gap-2">
        <textarea
          ref={textareaRef}
          className="flex-1 bg-input border border-border rounded-md py-1.5 px-2.5
                     text-[15px] text-foreground resize-none outline-none
                     h-8 max-h-[120px] leading-[1.4] transition-colors
                     focus:border-accent-blue/40"
          placeholder="Send a message to Claude… (Ctrl+Enter)"
          disabled={disabled}
          onKeyDown={handleKeyDown}
          onChange={handleChange}
        />
        <button
          className="self-end h-8 px-3 text-sm
                     border border-accent-blue bg-accent-blue text-white
                     hover:bg-accent-blue/90 rounded-md transition-colors
                     disabled:opacity-50 disabled:cursor-not-allowed"
          disabled={disabled}
          onClick={handleSend}
        >
          Send
        </button>
      </div>
    </div>
  );
}
