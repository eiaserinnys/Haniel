// Haniel Dashboard App
// Uses Phase 1 API types: RunnerStatus, WsEvent

import { Wifi, WifiOff, RefreshCw } from 'lucide-react'
import { useServices } from '@/hooks/useServices'
import { ServiceList } from '@/components/ServiceList'
import { api } from '@/lib/api'
import { useEffect, useState } from 'react'
import './index.css'

/** Returns seconds until the next poll based on last_poll + poll_interval. */
function useNextPollCountdown(
  lastPoll: string | null,
  pollInterval: number,
): number {
  const [secs, setSecs] = useState(0)

  useEffect(() => {
    if (!lastPoll || pollInterval <= 0) return

    const tick = () => {
      const elapsed = (Date.now() - new Date(lastPoll).getTime()) / 1000
      setSecs(Math.max(0, Math.ceil(pollInterval - elapsed)))
    }

    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [lastPoll, pollInterval])

  return secs
}

export default function App() {
  const {
    status,
    loading,
    error,
    wsStatus,
    controlService,
    pullRepo,
    approveSelfUpdate,
    dismissSelfUpdate,
    refreshStatus,
  } = useServices()

  const countdown = useNextPollCountdown(
    status?.last_poll ?? null,
    status?.poll_interval ?? 0,
  )

  return (
    <div className="min-h-screen bg-zinc-900 text-zinc-100">
      {/* Self-update banner */}
      {status?.self_update?.pending && (
        <div className="bg-blue-900/40 border-b border-blue-700/50 px-4 py-2 flex items-center justify-between">
          <span className="text-sm text-blue-300">
            A self-update is available for <strong>{status.self_update.repo}</strong>.
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={dismissSelfUpdate}
              className="text-sm text-blue-400 hover:text-blue-200 px-2 py-1 transition-colors"
            >
              Dismiss
            </button>
            <button
              onClick={approveSelfUpdate}
              className="text-sm bg-blue-600 hover:bg-blue-500 text-white px-3 py-1 rounded transition-colors"
            >
              Approve update
            </button>
          </div>
        </div>
      )}

      {/* Header */}
      <header className="border-b border-zinc-800 px-4 py-3 flex items-center gap-3">
        <h1 className="font-semibold text-zinc-100 flex-1">Haniel Dashboard</h1>
        <WsIndicator status={wsStatus} />
        <button
          onClick={() => { api.reload().catch(() => null) }}
          title="Reload config"
          className="p-1.5 text-zinc-400 hover:text-zinc-200 transition-colors"
        >
          <RefreshCw size={15} />
        </button>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-6 space-y-6">
        {/* Error banner */}
        {error && (
          <div className="bg-red-900/30 border border-red-700/50 rounded-lg px-4 py-2 text-sm text-red-300">
            {error}
          </div>
        )}

        {/* Loading state */}
        {loading && (
          <div className="text-center text-zinc-500 py-12">Connecting…</div>
        )}

        {/* Services + Repos */}
        {status && !loading && (
          <>
            <section>
              <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wider mb-3">
                Services
              </h2>
              <ServiceList status={status} onControl={controlService} />
            </section>

            {/* Repos */}
            {Object.keys(status.repos).length > 0 && (
              <section>
                <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wider mb-3">
                  Repositories
                </h2>
                <div className="space-y-2">
                  {Object.entries(status.repos).map(([name, repo]) => (
                    <div
                      key={name}
                      className="rounded-lg border border-zinc-700 bg-zinc-800/50 px-4 py-3"
                    >
                      <div className="flex items-center justify-between">
                        <div>
                          <span className="font-medium text-zinc-200">{name}</span>
                          <span className="ml-2 text-xs text-zinc-500">
                            {repo.branch} · {repo.last_head ?? '—'}
                          </span>
                        </div>
                        {repo.pending_changes && (
                          <button
                            onClick={() => pullRepo(name)}
                            className="text-xs bg-blue-600/20 text-blue-400 hover:bg-blue-600/40 px-2 py-1 rounded transition-colors"
                          >
                            Pull ({repo.pending_changes.commits.length} commits)
                          </button>
                        )}
                      </div>
                      {repo.fetch_error && (
                        <div className="mt-1 text-xs text-red-400">
                          Fetch error: {repo.fetch_error}
                        </div>
                      )}
                      {repo.pending_changes && (
                        <details className="mt-2 text-xs text-zinc-400">
                          <summary className="cursor-pointer hover:text-zinc-300">
                            {repo.pending_changes.commits.length} pending commits
                          </summary>
                          <pre className="mt-1 bg-zinc-900 rounded p-2 overflow-x-auto">
                            {repo.pending_changes.commits.join('\n')}
                          </pre>
                          {repo.pending_changes.stat && (
                            <pre className="mt-1 bg-zinc-900 rounded p-2 overflow-x-auto">
                              {repo.pending_changes.stat}
                            </pre>
                          )}
                        </details>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {/* Runner info footer */}
            <section className="text-xs text-zinc-600 border-t border-zinc-800 pt-4">
              <div className="flex gap-6 flex-wrap items-center">
                <span>Poll count: {status.poll_count}</span>
                <span>Interval: {status.poll_interval}s</span>
                {status.last_poll && (
                  <>
                    <span>Last poll: {new Date(status.last_poll).toLocaleTimeString()}</span>
                    <span className={countdown <= 5 ? 'text-zinc-400' : ''}>
                      Next in: {countdown}s
                    </span>
                  </>
                )}
                <button
                  onClick={refreshStatus}
                  title="Fetch status now"
                  className="flex items-center gap-1 text-zinc-500 hover:text-zinc-300 transition-colors"
                >
                  <RefreshCw size={11} />
                  Fetch now
                </button>
                {status.pending_restarts.length > 0 && (
                  <span className="text-yellow-500">
                    Pending restarts: {status.pending_restarts.join(', ')}
                  </span>
                )}
              </div>
            </section>
          </>
        )}
      </main>
    </div>
  )
}

function WsIndicator({ status }: { status: string }) {
  const connected = status === 'connected'
  return (
    <span
      title={`WebSocket: ${status}`}
      className={`flex items-center gap-1 text-xs ${
        connected ? 'text-green-400' : 'text-yellow-400'
      }`}
    >
      {connected ? <Wifi size={13} /> : <WifiOff size={13} />}
      {status}
    </span>
  )
}
