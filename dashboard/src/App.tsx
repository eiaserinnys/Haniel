// Haniel Dashboard App
// Uses Phase 1 API types: RunnerStatus, WsEvent

import { Wifi, WifiOff, RefreshCw, Plus } from 'lucide-react'
import { useServices } from '@/hooks/useServices'
import { ServiceList } from '@/components/ServiceList'
import { ServiceEditor } from '@/components/ServiceEditor'
import { RepoEditor } from '@/components/RepoEditor'
import { DependencyGraph } from '@/components/DependencyGraph'
import { ChatPanel } from '@/components/ChatPanel'
import { ResizablePanelGroup, ResizablePanel, ResizableHandle } from '@/components/ui/resizable'
import { api } from '@/lib/api'
import type { ServiceConfig, ServiceConfigInput, RepoConfigInput } from '@/lib/types'
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

// ── Editor dialog state types ─────────────────────────────────────────────────

interface ServiceEditorState {
  open: boolean
  editName?: string
  editConfig?: ServiceConfig
}

interface RepoEditorState {
  open: boolean
  editName?: string
  editConfig?: RepoConfigInput
}

export default function App() {
  const {
    status,
    loading,
    error,
    wsStatus,
    controlService,
    pullRepo,
    pullingRepos,
    approveSelfUpdate,
    dismissSelfUpdate,
    refreshStatus,
    updating,
  } = useServices()

  const countdown = useNextPollCountdown(
    status?.last_poll ?? null,
    status?.poll_interval ?? 0,
  )

  const [svcEditor, setSvcEditor] = useState<ServiceEditorState>({ open: false })
  const [repoEditor, setRepoEditor] = useState<RepoEditorState>({ open: false })
  const [crudError, setCrudError] = useState<string | null>(null)

  // ── Service CRUD handlers ──────────────────────────────────────────────────

  const handleAddService = () => {
    setSvcEditor({ open: true })
  }

  const handleEditService = (name: string) => {
    const cfg = status?.services[name]?.config
    setSvcEditor({ open: true, editName: name, editConfig: cfg })
  }

  const handleDeleteService = async (name: string) => {
    if (!window.confirm(`서비스 "${name}"을 삭제하시겠습니까?`)) return
    try {
      await api.deleteService(name)
      setCrudError(null)
      refreshStatus()
    } catch (e) {
      setCrudError(e instanceof Error ? e.message : String(e))
    }
  }

  const handleSaveService = async (name: string, config: ServiceConfigInput) => {
    if (svcEditor.editName) {
      await api.updateService(name, config)
    } else {
      await api.createService(name, config)
    }
    setCrudError(null)
    setSvcEditor({ open: false })
    refreshStatus()
  }

  // ── Repo CRUD handlers ────────────────────────────────────────────────────

  const handleAddRepo = () => {
    setRepoEditor({ open: true })
  }

  const handleEditRepo = async (name: string) => {
    try {
      const repos = await api.getConfigRepos()
      const cfg = repos[name]
      setRepoEditor({ open: true, editName: name, editConfig: cfg })
    } catch (e) {
      setCrudError(e instanceof Error ? e.message : String(e))
    }
  }

  const handleDeleteRepo = async (name: string) => {
    if (!window.confirm(`리포 "${name}"을 삭제하시겠습니까?`)) return
    try {
      await api.deleteRepo(name)
      setCrudError(null)
      refreshStatus()
    } catch (e) {
      setCrudError(e instanceof Error ? e.message : String(e))
    }
  }

  const handleSaveRepo = async (name: string, config: RepoConfigInput) => {
    if (repoEditor.editName) {
      await api.updateRepo(name, config)
    } else {
      await api.createRepo(name, config)
    }
    setCrudError(null)
    setRepoEditor({ open: false })
    refreshStatus()
  }

  // ── Derived values ─────────────────────────────────────────────────────────

  const availableRepos = status ? Object.keys(status.repos) : []
  const availableServices = status ? Object.keys(status.services) : []

  return (
    <div className="h-screen flex flex-col bg-zinc-900 text-zinc-100">
      {/* Self-update banner */}
      {status?.self_update?.pending && (
        <div className="bg-blue-900/40 border-b border-blue-700/50 px-4 py-2 flex items-center justify-between shrink-0">
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
      <header className="border-b border-zinc-800 px-4 py-3 flex items-center gap-3 shrink-0">
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

      {/* Main body: 2-panel split */}
      <ResizablePanelGroup orientation="horizontal" className="flex-1 overflow-hidden">
        <ResizablePanel defaultSize={60} minSize={20}>
          <div className="h-full overflow-y-auto">
            <main className="max-w-4xl mx-auto px-4 py-6 space-y-6">
        {/* Error banner */}
        {(error || crudError) && (
          <div className="bg-red-900/30 border border-red-700/50 rounded-lg px-4 py-2 text-sm text-red-300">
            {error || crudError}
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
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wider">
                  Services
                </h2>
                <button
                  onClick={handleAddService}
                  className="flex items-center gap-1 text-xs text-zinc-400 hover:text-zinc-200 transition-colors px-2 py-1 rounded hover:bg-zinc-800"
                >
                  <Plus size={12} />
                  서비스 추가
                </button>
              </div>
              <ServiceList
                status={status}
                onControl={controlService}
                onEdit={handleEditService}
                onDelete={handleDeleteService}
              />
            </section>

            {/* Repos */}
            <section>
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wider">
                  Repositories
                </h2>
                <button
                  onClick={handleAddRepo}
                  className="flex items-center gap-1 text-xs text-zinc-400 hover:text-zinc-200 transition-colors px-2 py-1 rounded hover:bg-zinc-800"
                >
                  <Plus size={12} />
                  리포 추가
                </button>
              </div>
              {Object.keys(status.repos).length === 0 ? (
                <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-8 text-center text-zinc-500 text-sm">
                  No repositories configured.
                </div>
              ) : (
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
                        <div className="flex items-center gap-2">
                          {pullingRepos.has(name) ? (
                            <span className="text-xs text-blue-400/60 px-2 py-1 animate-pulse">
                              Pulling…
                            </span>
                          ) : repo.pending_changes && (
                            <button
                              onClick={() => pullRepo(name)}
                              className="text-xs bg-blue-600/20 text-blue-400 hover:bg-blue-600/40 px-2 py-1 rounded transition-colors"
                            >
                              Pull ({repo.pending_changes.commits.length} commits)
                            </button>
                          )}
                          <button
                            onClick={() => handleEditRepo(name)}
                            className="text-xs text-zinc-500 hover:text-zinc-300 px-2 py-1 transition-colors"
                          >
                            편집
                          </button>
                          <button
                            onClick={() => handleDeleteRepo(name)}
                            className="text-xs text-zinc-500 hover:text-red-400 px-2 py-1 transition-colors"
                          >
                            삭제
                          </button>
                        </div>
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
              )}
            </section>

            {/* Dependency Graph */}
            {status.dependency_graph && Object.keys(status.dependency_graph).length > 0 && (
              <section>
                <h2 className="text-sm font-medium text-zinc-400 uppercase tracking-wider mb-3">
                  Dependency Graph
                </h2>
                <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
                  <DependencyGraph graph={status.dependency_graph} />
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
        </ResizablePanel>

        <ResizableHandle />

        <ResizablePanel defaultSize={40} minSize={15}>
          <ChatPanel />
        </ResizablePanel>
      </ResizablePanelGroup>

      {/* ServiceEditor dialog */}
      {svcEditor.open && (
        <ServiceEditor
          editName={svcEditor.editName}
          editConfig={svcEditor.editConfig}
          availableRepos={availableRepos}
          availableServices={availableServices}
          onSave={handleSaveService}
          onCancel={() => setSvcEditor({ open: false })}
        />
      )}

      {/* RepoEditor dialog */}
      {repoEditor.open && (
        <RepoEditor
          editName={repoEditor.editName}
          editConfig={repoEditor.editConfig}
          onSave={handleSaveRepo}
          onCancel={() => setRepoEditor({ open: false })}
        />
      )}

      {/* Self-update overlay */}
      {updating && <UpdateOverlay />}
    </div>
  )
}

const UPDATE_POLL_INTERVAL_MS = 2000
const UPDATE_TIMEOUT_MS = 120_000

function UpdateOverlay() {
  const [timedOut, setTimedOut] = useState(false)

  useEffect(() => {
    const start = Date.now()
    const id = setInterval(async () => {
      if (Date.now() - start > UPDATE_TIMEOUT_MS) {
        clearInterval(id)
        setTimedOut(true)
        return
      }
      try {
        await api.getStatus()
        clearInterval(id)
        window.location.reload()
      } catch {
        // Server still down — retry on next tick
      }
    }, UPDATE_POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div className="relative bg-zinc-800 border border-zinc-700 rounded-xl px-8 py-6 flex flex-col items-center gap-4 shadow-2xl">
        {timedOut ? (
          <>
            <WifiOff className="text-yellow-400" size={32} />
            <div className="text-zinc-100 font-medium">서버 응답 없음</div>
            <div className="text-zinc-400 text-sm">수동으로 새로고침해 주세요.</div>
            <button
              onClick={() => window.location.reload()}
              className="text-sm bg-zinc-700 hover:bg-zinc-600 text-zinc-200 px-4 py-1.5 rounded transition-colors"
            >
              새로고침
            </button>
          </>
        ) : (
          <>
            <RefreshCw className="animate-spin text-blue-400" size={32} />
            <div className="text-zinc-100 font-medium">Updating…</div>
            <div className="text-zinc-400 text-sm">서버가 재시작되면 자동으로 새로고침됩니다.</div>
          </>
        )}
      </div>
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
