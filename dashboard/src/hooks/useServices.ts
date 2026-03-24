// useServices — WebSocket + service state management
// Uses Phase 1 API types: RunnerStatus, WsEvent

import { useState, useCallback } from 'react'
import type {
  RunnerStatus,
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
              : { repo: event.repo, pending: true, auto_update: false },
          }
        })
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
  }, [])

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
      setUpdating(true)
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
  }
}
