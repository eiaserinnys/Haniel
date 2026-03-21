import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { ChatSession } from "@/hooks/useChatWebSocket";

interface SessionListModalProps {
  open: boolean;
  sessions: ChatSession[];
  onSelect: (sessionId: string) => void;
  onClose: () => void;
}

export function SessionListModal({
  open,
  sessions,
  onSelect,
  onClose,
}: SessionListModalProps) {
  const sorted = [...sessions].sort(
    (a, b) =>
      new Date(b.last_active_at).getTime() -
      new Date(a.last_active_at).getTime()
  );

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="max-w-md bg-background border-border">
        <DialogHeader>
          <DialogTitle className="text-foreground">Sessions</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-1 max-h-[60vh] overflow-y-auto">
          {sorted.length === 0 && (
            <p className="text-sm text-muted-foreground px-2 py-3">
              No sessions found.
            </p>
          )}
          {sorted.map((session) => (
            <button
              key={session.id}
              className="text-left px-3 py-2 rounded-md hover:bg-muted transition-colors"
              onClick={() => {
                onSelect(session.id);
                onClose();
              }}
            >
              <div className="text-xs text-muted-foreground">
                {new Date(session.last_active_at).toLocaleString()}
              </div>
              {session.preview && (
                <div className="text-sm text-foreground truncate mt-0.5">
                  {session.preview}
                </div>
              )}
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  );
}
