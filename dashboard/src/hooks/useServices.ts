// useServices — WebSocket + service state management
// Uses Phase 1 API types: RunnerStatus, WsEvent

import { useState, useCallback } from 'react'
import type {
  RunnerStatus,
  WsEvent,
  WsStateChangeEvent,
  WsRepoChangeEvent,
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

  const pullRepo = useCallback(async (name: string) => {
    setPullingRepos(prev => new Set(prev).add(name))
    try {
      const result = await api.pullRepo(name)
      // 즉시 상태 갱신: pending_changes 제거 + head 업데이트
      setStatus(prev => {
        if (!prev) return prev
        const repos = { ...prev.repos }
        if (repos[name]) {
          repos[name] = { ...repos[name], pending_changes: null, last_head: result.head ?? repos[name].last_head }
        }
        return { ...prev, repos }
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setPullingRepos(prev => {
        const next = new Set(prev)
        next.delete(name)
        return next
      })
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
