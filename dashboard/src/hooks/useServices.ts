// useServices — WebSocket + service state management
// Uses Phase 1 API types: RunnerStatus, WsEvent

import { useState, useCallback, useRef } from 'react'
import type {
  RunnerStatus,
  SelfUpdateResult,
  WsEvent,
  WsStateChangeEvent,
  WsRepoChangeEvent,
  WsRepoPullingEvent,
} from '@/lib/types'
import { api } from '@/lib/api'
import { useWebSocket } from './useWebSocket'

export function useServices() {
  const [status, setStatus] = useState<RunnerStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updating, setUpdating] = useState(false)
  const [updateResult, setUpdateResult] = useState<SelfUpdateResult | null>(null)
  // Track when overlay opened. null until self_update_started arrives.
  // Used to filter stale last_result in self_update_completed events
  // (e.g. duplicate broadcasts within the same runner instance) and to
  // distinguish init-time recovery from in-session completion.
  const updateStartedAtRef = useRef<number | null>(null)

  const applyResultOutcome = useCallback((result: SelfUpdateResult) => {
    setUpdateResult(result)
    if (result.ok) {
      // Defer reload slightly so React commits state before navigation.
      window.setTimeout(() => window.location.reload(), 200)
    } else {
      const msg = result.error ?? 'Self-update failed (no error message).'
      setError(`Self-update failed: ${msg}`)
      setUpdating(false)
    }
  }, [])

  const processCompletedResult = useCallback((result: SelfUpdateResult) => {
    // Stale-result filter: only act on results finished AFTER overlay opened.
    // Used by the self_update_completed broadcast — overlay was opened by
    // self_update_started so updateStartedAtRef is set.
    const started = updateStartedAtRef.current
    if (started === null) return
    const finishedMs = Date.parse(result.finished_at)
    if (Number.isNaN(finishedMs) || finishedMs < started) return
    applyResultOutcome(result)
  }, [applyResultOutcome])

  const processInitRecoveryResult = useCallback((result: SelfUpdateResult) => {
    // Init recovery: last_result was loaded from a marker before this client
    // connected. There is no overlay open and updateStartedAtRef is null,
    // because self_update_started broadcast was lost during server restart.
    // We still must surface the outcome (req: 실패 시 명확한 에러 표시).
    // - On failure: show error in main UI (no overlay flash).
    // - On success: silently reload — the previous cycle succeeded and
    //   the user just connecting now does not need to see "Updating…".
    if (updateStartedAtRef.current !== null) return  // already handled by completed path
    applyResultOutcome(result)
  }, [applyResultOutcome])

  const handleEvent = useCallback((event: WsEvent) => {
    switch (event.type) {
      case 'init': {
        setStatus(event.status)
        // Sync pullingRepos from server state (handles WS reconnection recovery)
        const pulling = new Set(
          Object.entries(event.status.repos)
            .filter(([, r]) => r.pulling)
            .map(([name]) => name)
        )
        setPullingRepos(pulling)
        setLoading(false)
        setError(null)
        // Recover from a self_update_started broadcast that was lost during
        // server restart. Without this, a failed self-update would be
        // silently swallowed.
        const lr = event.status.self_update?.last_result ?? null
        if (lr !== null) processInitRecoveryResult(lr)
        break
      }
      case 'state_change': {
        const e = event as WsStateChangeEvent
        setStatus((prev) => {
          if (!prev) return prev
          const svc = prev.services[e.service]
          if (!svc) return prev
          return {
            ...prev,
            services: {
              ...prev.services,
              [e.service]: { ...svc, state: e.new },
            },
          }
        })
        break
      }
      case 'repo_change': {
        const e = event as WsRepoChangeEvent
        setStatus((prev) => {
          if (!prev) return prev
          const repo = prev.repos[e.repo]
          if (!repo) return prev
          return {
            ...prev,
            repos: {
              ...prev.repos,
              [e.repo]: { ...repo, pending_changes: e.pending_changes },
            },
          }
        })
        break
      }
      case 'self_update_pending': {
        setStatus((prev) => {
          if (!prev) return prev
          return {
            ...prev,
            self_update: prev.self_update
              ? { ...prev.self_update, pending: true }
              : { repo: event.repo, pending: true, auto_update: false, last_result: null },
          }
        })
        break
      }
      case 'self_update_started': {
        updateStartedAtRef.current = Date.now()
        setUpdating(true)
        setUpdateResult(null)
        break
      }
      case 'self_update_completed': {
        processCompletedResult(event.result)
        break
      }
      case 'repo_pulling': {
        const e = event as WsRepoPullingEvent
        setPullingRepos(prev => {
          const next = new Set(prev)
          e.is_pulling ? next.add(e.repo) : next.delete(e.repo)
          return next
        })
        // Pull complete: refresh full status to sync pending_changes etc.
        if (!e.is_pulling) {
          api.getStatus().then(setStatus).catch(() => null)
        }
        break
      }
      case 'reload_complete': {
        api.getStatus().then(setStatus).catch(() => null)
        break
      }
    }
  }, [processCompletedResult, processInitRecoveryResult])

  const { connectionState } = useWebSocket({ onEvent: handleEvent })

  const controlService = useCallback(
    async (name: string, action: 'start' | 'stop' | 'restart' | 'enable') => {
      try {
        switch (action) {
          case 'start':   await api.startService(name); break
          case 'stop':    await api.stopService(name); break
          case 'restart': await api.restartService(name); break
          case 'enable':  await api.enableService(name); break
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    },
    [],
  )

  const [pullingRepos, setPullingRepos] = useState<Set<string>>(new Set())

  // pullRepo only triggers the REST call — pulling state is managed
  // exclusively via WS repo_pulling events (single source of truth).
  // This ensures consistent UI whether pull is triggered from dashboard,
  // Slack approval, or auto_apply.
  const pullRepo = useCallback(async (name: string) => {
    try {
      await api.pullRepo(name)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const approveSelfUpdate = useCallback(async () => {
    try {
      await api.approveSelfUpdate()
      // Overlay is opened by the WS self_update_started event
      // (canonical 'work in progress' signal). API response alone
      // is insufficient — see ADR-0002 result propagation.
      // If the broadcast is lost (rare), init-time last_result recovery
      // (case 'init') will surface success/failure when WS reconnects.
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const selfRestart = useCallback(async () => {
    try {
      await api.selfRestart()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const refreshStatus = useCallback(async () => {
    try {
      const s = await api.getStatus()
      setStatus(s)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  return {
    status,
    loading,
    error,
    wsStatus: connectionState,
    controlService,
    pullRepo,
    pullingRepos,
    approveSelfUpdate,
    selfRestart,
    refreshStatus,
    updating,
    updateResult,
  }
}
