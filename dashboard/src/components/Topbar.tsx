import { useState } from 'react'
import { Icon } from './Icon'
import { Mark } from './Status'
import { ThemeMenu } from './ThemeMenu'
import { api } from '@/lib/api'
import { errorMessage } from '@/lib/utils'
import type { ConnectionState } from '@/hooks/useWebSocket'

function ConnectionMark({ state }: { state: ConnectionState }) {
  switch (state) {
    case 'connected':
      return <Mark kind="ok" label="Connected" />
    case 'connecting':
      return <Mark kind="busy" label="Connecting…" />
    case 'reconnecting':
      return <Mark kind="busy" label="Reconnecting…" />
    case 'disconnected':
      return <Mark kind="fail" label="Disconnected" />
  }
}

interface TopbarProps {
  connection: ConnectionState
  onOpenMenu: () => void
  onError: (message: string) => void
}

export function Topbar({ connection, onOpenMenu, onError }: TopbarProps) {
  const [reloading, setReloading] = useState(false)

  const reload = async () => {
    if (reloading) return
    setReloading(true)
    try {
      await api.reload()
    } catch (e) {
      onError(errorMessage(e))
    } finally {
      setReloading(false)
    }
  }

  return (
    <header className="topbar">
      <button type="button" className="icon-btn menu-btn" aria-label="Open menu" onClick={onOpenMenu}>
        <Icon name="menu" />
      </button>
      <div className="brand">
        <span className="brand-name">Haniel</span>
        <span className="brand-sub">Local</span>
      </div>
      <div className="topbar-end">
        <span className="conn" role="status">
          <ConnectionMark state={connection} />
        </span>
        <button
          type="button"
          className="icon-btn"
          aria-label="Reload config"
          title="Reload config"
          aria-disabled={reloading || undefined}
          onClick={reload}
        >
          <Icon name="reload" className={reloading ? 'spin' : undefined} />
        </button>
        <ThemeMenu />
      </div>
    </header>
  )
}
