import { useCallback, useEffect, useRef, useState } from 'react'
import { Icon } from './Icon'
import { Notice, SkeletonRows } from './states'
import { getServiceLogs } from '@/lib/api'
import { errorMessage } from '@/lib/utils'

type LogFetchFn = (name: string, count: number) => Promise<{ lines: string[] }>

interface LogViewerProps {
  serviceName: string
  initialLines?: number
  fetchFn?: LogFetchFn
}

const POLL_INTERVAL_MS = 3000
const LINE_COUNTS = [50, 100, 200, 500]
const ERROR_LINE = /\b(ERROR|CRITICAL|FATAL)\b/

export function LogViewer({ serviceName, initialLines = 100, fetchFn = getServiceLogs }: LogViewerProps) {
  const [lines, setLines] = useState<string[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lineCount, setLineCount] = useState(initialLines)
  const paneRef = useRef<HTMLPreElement>(null)
  const scrolledRef = useRef(false)

  const fetchLogs = useCallback(
    async (silent: boolean) => {
      if (!silent) setLoading(true)
      try {
        const result = await fetchFn(serviceName, lineCount)
        setLines(result.lines)
        setError(null)
      } catch (e) {
        setError(errorMessage(e))
      } finally {
        if (!silent) setLoading(false)
      }
    },
    [fetchFn, serviceName, lineCount],
  )

  useEffect(() => {
    void fetchLogs(false)
    const id = setInterval(() => void fetchLogs(true), POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [fetchLogs])

  useEffect(() => {
    const pane = paneRef.current
    if (lines && lines.length > 0 && pane && !scrolledRef.current) {
      scrolledRef.current = true
      pane.scrollTop = pane.scrollHeight
    }
  }, [lines])

  return (
    <div className="logs">
      <div className="log-bar">
        <span>Last</span>
        <span className="seg" role="group" aria-label="Lines">
          {LINE_COUNTS.map((n) => (
            <button key={n} type="button" aria-pressed={lineCount === n} onClick={() => setLineCount(n)}>
              {n}
            </button>
          ))}
        </span>
        <button
          type="button"
          className="icon-btn"
          aria-label="Refresh logs"
          title="Refresh"
          aria-disabled={loading || undefined}
          onClick={() => !loading && void fetchLogs(false)}
        >
          <Icon name="reload" className={loading ? 'spin' : undefined} />
        </button>
      </div>
      {error ? (
        <Notice summary="Couldn't read logs" detail={error} onRetry={() => void fetchLogs(false)} />
      ) : lines === null ? (
        <div aria-busy="true">
          <SkeletonRows count={2} />
        </div>
      ) : lines.length === 0 ? (
        <p className="empty-text">No logs yet.</p>
      ) : (
        <pre ref={paneRef} className="log-pane scroll" tabIndex={0} aria-label={`Log output of ${serviceName}`}>
          {lines.map((line, i) => (
            <span key={i} className={ERROR_LINE.test(line) ? 'lv-err' : undefined}>
              {line}
              {'\n'}
            </span>
          ))}
        </pre>
      )}
    </div>
  )
}
