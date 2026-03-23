import { useState, useEffect, useRef } from "react";
import { RefreshCw } from "lucide-react";
import { getServiceLogs } from "@/lib/api";
import { cn } from "@/lib/utils";

type LogFetchFn = (name: string, count: number) => Promise<{ lines: string[] }>;

interface LogViewerProps {
  serviceName: string;
  initialLines?: number;
  fetchFn?: LogFetchFn;
}

const POLL_INTERVAL_MS = 3000;

export function LogViewer({ serviceName, initialLines = 100, fetchFn = getServiceLogs }: LogViewerProps) {
  const [lines, setLines] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lineCount, setLineCount] = useState(initialLines);
  const containerRef = useRef<HTMLDivElement>(null);
  const isFirstLoad = useRef(true);

  const scrollToBottom = () => {
    const el = containerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  const fetchLogs = async (count: number, silent = false) => {
    if (!silent) setLoading(true);
    setError(null);
    try {
      const result = await fetchFn(serviceName, count);
      setLines(result.lines);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to fetch logs");
    } finally {
      if (!silent) setLoading(false);
    }
  };

  // serviceName이 바뀌면 초기 로드
  useEffect(() => {
    isFirstLoad.current = true;
    fetchLogs(lineCount);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceName]);

  // 폴링
  useEffect(() => {
    const id = setInterval(() => fetchLogs(lineCount, true), POLL_INTERVAL_MS);
    return () => clearInterval(id);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serviceName, lineCount]);

  // 로그 갱신 시 컨테이너 내부만 스크롤 (초기 로드 시만)
  useEffect(() => {
    if (lines.length > 0 && isFirstLoad.current) {
      isFirstLoad.current = false;
      scrollToBottom();
    }
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
      <div ref={containerRef} className="max-h-80 overflow-y-auto bg-zinc-950 p-4 font-mono text-xs leading-relaxed">
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
      </div>
    </div>
  );
}
