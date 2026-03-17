import { useState, useEffect, useRef } from "react";
import { RefreshCw } from "lucide-react";
import { getServiceLogs } from "@/lib/api";
import { cn } from "@/lib/utils";

interface LogViewerProps {
  serviceName: string;
  initialLines?: number;
}

export function LogViewer({ serviceName, initialLines = 100 }: LogViewerProps) {
  const [lines, setLines] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lineCount, setLineCount] = useState(initialLines);
  const bottomRef = useRef<HTMLDivElement>(null);

  const fetchLogs = async (count: number) => {
    setLoading(true);
    setError(null);
    try {
      const result = await getServiceLogs(serviceName, count);
      setLines(result.lines);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to fetch logs");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLogs(lineCount);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceName]);

  // Auto-scroll to bottom when lines update
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [lines]);

  const handleLineCountChange = (count: number) => {
    setLineCount(count);
    fetchLogs(count);
  };

  return (
    <div className="flex flex-col">
      {/* Toolbar */}
      <div className="flex items-center gap-3 px-4 py-2 border-b border-zinc-800 text-xs text-zinc-400">
        <span>Last</span>
        {[50, 100, 200, 500].map((n) => (
          <button
            key={n}
            className={cn(
              "px-2 py-0.5 rounded transition-colors",
              lineCount === n
                ? "bg-zinc-700 text-zinc-100"
                : "hover:bg-zinc-800"
            )}
            onClick={() => handleLineCountChange(n)}
          >
            {n}
          </button>
        ))}
        <span>lines</span>
        <button
          className="ml-auto hover:text-zinc-200 transition-colors disabled:opacity-50"
          onClick={() => fetchLogs(lineCount)}
          disabled={loading}
          title="Refresh"
        >
          <RefreshCw size={13} className={cn(loading && "animate-spin")} />
        </button>
      </div>

      {/* Log content */}
      <div className="max-h-80 overflow-y-auto bg-zinc-950 p-4 font-mono text-xs leading-relaxed">
        {error ? (
          <p className="text-red-400">{error}</p>
        ) : lines.length === 0 ? (
          <p className="text-zinc-600">{loading ? "Loading…" : "No logs available."}</p>
        ) : (
          <>
            {lines.map((line, i) => (
              <div key={i} className="text-zinc-300 whitespace-pre-wrap break-all">
                {line}
              </div>
            ))}
          </>
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
