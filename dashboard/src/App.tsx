// Haniel local dashboard (Index style): header bar, section sidebar, one ledger page.

import { Fragment, useMemo, useRef, useState } from 'react'
import { ConfirmDialog } from '@/components/ConfirmDialog'
import { DependencyGraph } from '@/components/DependencyGraph'
import { Drawer } from '@/components/Drawer'
import { Icon } from '@/components/Icon'
import { RepoEditor } from '@/components/RepoEditor'
import { RepoGroupRow, RepoRow, SelfRow } from '@/components/RepoRows'
import { SidePanel, type SectionId } from '@/components/SectionNav'
import { ServiceEditor } from '@/components/ServiceEditor'
import { ServiceRow } from '@/components/ServiceRow'
import { LoadError, Notice, SkeletonRows } from '@/components/states'
import { Topbar } from '@/components/Topbar'
import { UpdateOverlay } from '@/components/UpdateOverlay'
import { useCurrentSection } from '@/hooks/useCurrentSection'
import { useMediaQuery } from '@/hooks/useMediaQuery'
import { useServices } from '@/hooks/useServices'
import { api } from '@/lib/api'
import { groupServicesAndRepos } from '@/lib/groups'
import type { RepoConfigInput, ServiceConfig, ServiceConfigInput } from '@/lib/types'
import { errorMessage } from '@/lib/utils'

const SECTION_IDS = ['services', 'repos', 'deps'] as const

type EditorState<T> = { open: false } | { open: true; editName?: string; editConfig?: T }

type ConfirmState = { kind: 'service' | 'repo' | 'restart'; name: string } | null

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
    selfRestart,
    refreshStatus,
    dismissError,
    dismissUpdate,
    updating,
    updateResult,
  } = useServices()

  const [svcEditor, setSvcEditor] = useState<EditorState<ServiceConfig>>({ open: false })
  const [repoEditor, setRepoEditor] = useState<EditorState<RepoConfigInput>>({ open: false })
  const [confirm, setConfirm] = useState<ConfirmState>(null)
  const [crudError, setCrudError] = useState<string | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const contentRef = useRef<HTMLDivElement>(null)
  const [section, jumpTo] = useCurrentSection<SectionId>(contentRef, SECTION_IDS)
  const phone = useMediaQuery('(max-width: 760px)', (matches) => {
    if (!matches) setDrawerOpen(false)
  })

  const groups = useMemo(() => (status ? groupServicesAndRepos(status, status.self_update?.repo) : null), [status])

  const failure = error ?? crudError
  const dismissFailure = () => {
    dismissError()
    setCrudError(null)
  }
  const reportError = (message: string) => setCrudError(message)

  // ── Service and repo CRUD ──────────────────────────────────────────────────

  const handleEditService = (name: string) =>
    setSvcEditor({ open: true, editName: name, editConfig: status?.services[name]?.config })

  const handleSaveService = async (name: string, config: ServiceConfigInput) => {
    if (svcEditor.open && svcEditor.editName) await api.updateService(name, config)
    else await api.createService(name, config)
    setCrudError(null)
    setSvcEditor({ open: false })
    void refreshStatus()
  }

  const handleEditRepo = async (name: string) => {
    try {
      const repos = await api.getConfigRepos()
      setRepoEditor({ open: true, editName: name, editConfig: repos[name] })
    } catch (e) {
      setCrudError(errorMessage(e))
    }
  }

  const handleSaveRepo = async (name: string, config: RepoConfigInput) => {
    if (repoEditor.open && repoEditor.editName) await api.updateRepo(name, config)
    else await api.createRepo(name, config)
    setCrudError(null)
    setRepoEditor({ open: false })
    void refreshStatus()
  }

  const runConfirmed = async () => {
    if (!confirm) return
    if (confirm.kind === 'service') await api.deleteService(confirm.name)
    else if (confirm.kind === 'repo') await api.deleteRepo(confirm.name)
    else await selfRestart()
    setCrudError(null)
    if (confirm.kind !== 'restart') void refreshStatus()
  }

  // A deleted row takes the opener with it; hand focus to its section instead of dropping it on the page.
  const closeConfirm = () => {
    const kind = confirm?.kind
    setConfirm(null)
    if (kind === 'restart' || !kind) return
    requestAnimationFrame(() => {
      if (document.activeElement === document.body) {
        document.getElementById(kind === 'service' ? 'sec-services' : 'sec-repos')?.focus()
      }
    })
  }

  const jump = (id: SectionId) => {
    setDrawerOpen(false)
    jumpTo(id)
  }

  // ── Derived values ─────────────────────────────────────────────────────────

  const availableRepos = status ? Object.keys(status.repos) : []
  const counts = {
    services: groups && status ? Object.keys(status.services).length : null,
    repos: groups ? groups.standaloneRepos.length : null,
  }
  const loadFailed = loading && (wsStatus === 'reconnecting' || wsStatus === 'disconnected')
  const side = (
    <SidePanel status={status} onFetch={refreshStatus} counts={counts} current={section} onJump={jump} />
  )

  return (
    <div className="app">
      <Topbar connection={loading && wsStatus === 'connected' ? 'connecting' : wsStatus} onOpenMenu={() => setDrawerOpen(true)} onError={reportError} />

      <div className="shell">
        <aside className="sidebar">{side}</aside>

        <div className="content scroll" ref={contentRef}>
          <main className="content-inner">
            <h1 className="sr-only">Haniel local dashboard</h1>

            {failure && !loadFailed && <Notice detail={failure} onDismiss={dismissFailure} />}

            <section className="sect" id="sec-services" tabIndex={-1} aria-labelledby="h-services" aria-busy={loading}>
              <div className="sect-head">
                <h2 className="sect-title" id="h-services">
                  Services
                </h2>
                <button type="button" className="btn btn-ghost edge" onClick={() => setSvcEditor({ open: true })}>
                  <Icon name="plus" small />
                  Add service
                </button>
              </div>
              {loadFailed ? (
                <div className="ledger">
                  <LoadError what="services" detail={error ?? 'WebSocket /ws is closed. Retrying.'} onRetry={() => void refreshStatus()} />
                </div>
              ) : !status || !groups ? (
                <>
                  <div className="ledger ledger-accent">
                    <SkeletonRows count={1} />
                  </div>
                  <div className="ledger">
                    <SkeletonRows count={4} />
                  </div>
                </>
              ) : (
                <>
                  {groups.selfRepo && status.self_update && (
                    <div className="ledger ledger-accent">
                      <SelfRow
                        repo={groups.selfRepo}
                        selfUpdate={status.self_update}
                        onApprove={approveSelfUpdate}
                        onRestart={() => setConfirm({ kind: 'restart', name: 'haniel' })}
                      />
                    </div>
                  )}
                  <div className="ledger">
                    {groups.repoGroups.map((group) => (
                      <Fragment key={group.repoName}>
                        <RepoGroupRow group={group} isPulling={pullingRepos.has(group.repoName)} onPull={pullRepo} />
                        {group.services.map(({ name, service }) => (
                          <ServiceRow
                            key={name}
                            name={name}
                            service={service}
                            onControl={controlService}
                            onEdit={handleEditService}
                            onDelete={(n) => setConfirm({ kind: 'service', name: n })}
                            hideRepo
                          />
                        ))}
                      </Fragment>
                    ))}
                    {groups.standaloneServices.map(({ name, service }) => (
                      <ServiceRow
                        key={name}
                        name={name}
                        service={service}
                        onControl={controlService}
                        onEdit={handleEditService}
                        onDelete={(n) => setConfirm({ kind: 'service', name: n })}
                      />
                    ))}
                    {groups.repoGroups.length === 0 && groups.standaloneServices.length === 0 && (
                      <div className="empty">
                        <p className="empty-title">No services</p>
                        <button type="button" className="btn btn-outline" onClick={() => setSvcEditor({ open: true })}>
                          <Icon name="plus" small />
                          Add service
                        </button>
                      </div>
                    )}
                  </div>
                </>
              )}
            </section>

            <section className="sect" id="sec-repos" tabIndex={-1} aria-labelledby="h-repos" aria-busy={loading}>
              <div className="sect-head">
                <h2 className="sect-title" id="h-repos">
                  Repositories
                </h2>
                <button type="button" className="btn btn-ghost edge" onClick={() => setRepoEditor({ open: true })}>
                  <Icon name="plus" small />
                  Add repo
                </button>
              </div>
              {!groups ? (
                loadFailed ? null : (
                  <div className="ledger">
                    <SkeletonRows count={2} />
                  </div>
                )
              ) : (
                <div className="ledger">
                  {groups.standaloneRepos.map((repo) => (
                    <RepoRow
                      key={repo.repoName}
                      repo={repo}
                      isPulling={pullingRepos.has(repo.repoName)}
                      onPull={pullRepo}
                      onEdit={handleEditRepo}
                      onDelete={(n) => setConfirm({ kind: 'repo', name: n })}
                    />
                  ))}
                  {groups.standaloneRepos.length === 0 && (
                    <div className="empty is-inline">
                      <p className="empty-text">No standalone repositories.</p>
                    </div>
                  )}
                </div>
              )}
            </section>

            <section className="sect" id="sec-deps" aria-labelledby="h-deps" aria-busy={loading}>
              <div className="sect-head">
                <h2 className="sect-title" id="h-deps">
                  Dependencies
                </h2>
              </div>
              {!status ? (
                loadFailed ? null : (
                  <div className="figure">
                    <div className="sk-lines">
                      <span className="sk w2" />
                      <span className="sk w1" />
                      <span className="sk w3" />
                    </div>
                  </div>
                )
              ) : status.dependency_graph && Object.keys(status.dependency_graph).length > 0 ? (
                <DependencyGraph graph={status.dependency_graph} />
              ) : (
                <div className="figure">
                  <p className="empty-text">No dependencies.</p>
                </div>
              )}
            </section>
          </main>
        </div>
      </div>

      {drawerOpen && phone && <Drawer onClose={() => setDrawerOpen(false)}>{side}</Drawer>}

      {svcEditor.open && (
        <ServiceEditor
          editName={svcEditor.editName}
          editConfig={svcEditor.editConfig}
          availableRepos={availableRepos}
          onSave={handleSaveService}
          onCancel={() => setSvcEditor({ open: false })}
        />
      )}

      {repoEditor.open && (
        <RepoEditor
          editName={repoEditor.editName}
          editConfig={repoEditor.editConfig}
          onSave={handleSaveRepo}
          onCancel={() => setRepoEditor({ open: false })}
        />
      )}

      {confirm && (
        <ConfirmDialog
          title={confirm.kind === 'restart' ? 'Restart Haniel?' : `Delete ${confirm.kind}?`}
          sub={
            confirm.kind === 'restart' ? (
              <span>Every service stops until Haniel is back.</span>
            ) : (
              <span className="mono">{confirm.name}</span>
            )
          }
          action={confirm.kind === 'restart' ? 'Restart' : 'Delete'}
          onConfirm={runConfirmed}
          onClose={closeConfirm}
        />
      )}

      {updating && <UpdateOverlay result={updateResult} onDismiss={dismissUpdate} />}
    </div>
  )
}
