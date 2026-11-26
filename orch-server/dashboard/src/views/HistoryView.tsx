import { useState, useMemo, type KeyboardEvent } from 'react';
import { Status } from '@/components/shared/Status';
import { Empty, LoadError, Skeleton } from '@/components/shared/LoadStates';
import { timeOfDay, dateLabel, durMs, parseCommit } from '@/lib/utils';
import type { Deploy, DeployStatus, LoadState } from '@/types';

interface HistoryViewProps {
  deploys: Deploy[];
  load?: LoadState;
  onRetry?: () => void;
  // Controlled by App.tsx so the toggle survives unmounts and the same
  // checkbox state drives the next fetchHistory query. Default off — auto-
  // supersede entries are noise unless the operator is auditing chains.
  includeSuperseded: boolean;
  onToggleSuperseded: (value: boolean) => void;
}

type FilterTab = 'all' | DeployStatus;

const TABS: { key: FilterTab; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'pending', label: 'Pending' },
  { key: 'deploying', label: 'Deploying' },
  { key: 'success', label: 'Success' },
  { key: 'failed', label: 'Failed' },
  { key: 'rejected', label: 'Rejected' },
];

const READY: LoadState = { status: 'ready' };

export function HistoryView({
  deploys,
  load = READY,
  onRetry = () => undefined,
  includeSuperseded,
  onToggleSuperseded,
}: HistoryViewProps) {
  const [activeTab, setActiveTab] = useState<FilterTab>('all');
  const [expandedErrors, setExpandedErrors] = useState<Set<string>>(new Set());

  const counts = useMemo(() => {
    const c = { all: deploys.length, pending: 0, deploying: 0, success: 0, failed: 0, rejected: 0 };
    for (const d of deploys) { c[d.status]++; }
    return c;
  }, [deploys]);

  const filtered = useMemo(() => {
    if (activeTab === 'all') return deploys;
    return deploys.filter(d => d.status === activeTab);
  }, [deploys, activeTab]);

  // Group by date
  const groups = useMemo(() => {
    const map = new Map<string, Deploy[]>();
    for (const d of filtered) {
      const label = dateLabel(d.created_at);
      const arr = map.get(label);
      if (arr) arr.push(d); else map.set(label, [d]);
    }
    return Array.from(map.entries());
  }, [filtered]);

  const toggleError = (id: string) => {
    setExpandedErrors(s => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  // Roving focus for the tab list: arrows, Home, End.
  const handleTabKeys = (e: KeyboardEvent<HTMLDivElement>) => {
    const index = TABS.findIndex(t => t.key === activeTab);
    let next: number;
    if (e.key === 'ArrowRight') next = (index + 1) % TABS.length;
    else if (e.key === 'ArrowLeft') next = (index - 1 + TABS.length) % TABS.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = TABS.length - 1;
    else return;
    e.preventDefault();
    setActiveTab(TABS[next].key);
    e.currentTarget.querySelectorAll<HTMLElement>('[role="tab"]')[next]?.focus();
  };

  const showTools = load.status === 'ready' && (deploys.length > 0 || includeSuperseded);

  let body;
  if (load.status === 'loading') body = <Skeleton rows={5} />;
  else if (load.status === 'error') body = <LoadError what="history" detail={load.detail} onRetry={onRetry} />;
  else if (deploys.length === 0) {
    body = <div className="ledger"><Empty title="No history" text="Deploy events show up here." /></div>;
  } else {
    body = (
      <div className="hist-wrap">
        <div className="tabs" role="tablist" aria-label="Status" onKeyDown={handleTabKeys}>
          {TABS.map(tab => (
            <button
              key={tab.key}
              type="button"
              role="tab"
              className="tab"
              aria-selected={activeTab === tab.key}
              tabIndex={activeTab === tab.key ? 0 : -1}
              onClick={() => setActiveTab(tab.key)}
            >
              {tab.label}
              <span className="nav-count">{counts[tab.key]}</span>
            </button>
          ))}
        </div>
        <div className="hist" role="tabpanel">
          {groups.length === 0 && (
            <div className="ledger"><Empty inline text="No events match this filter." /></div>
          )}
          {groups.map(([label, items]) => (
            <section key={label} aria-label={label}>
              <h2 className="group-label">{label}</h2>
              <div className="ledger">
                {items.map(item => (
                  <HistoryRow
                    key={item.deploy_id}
                    deploy={item}
                    showError={expandedErrors.has(item.deploy_id)}
                    onToggleError={() => toggleError(item.deploy_id)}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Deploy History</h1>
        {showTools && (
          <div className="page-tools">
            <label className="check-label">
              <input
                type="checkbox"
                className="check"
                checked={includeSuperseded}
                onChange={e => onToggleSuperseded(e.target.checked)}
              />
              Include superseded
            </label>
          </div>
        )}
      </div>
      {body}
    </>
  );
}

interface HistoryRowProps {
  deploy: Deploy;
  showError: boolean;
  onToggleError: () => void;
}

function HistoryRow({ deploy, showError, onToggleError }: HistoryRowProps) {
  const firstCommit = deploy.commits.length > 0 ? parseCommit(deploy.commits[0]) : null;
  const superseded = deploy.status === 'rejected' && Boolean(deploy.reject_reason?.startsWith('superseded by '));
  const detail = deploy.error || (!superseded ? deploy.reject_reason : null);
  const hasMeta = deploy.approved_by || superseded || detail;

  return (
    <div className="row h-row">
      <span className="h-time mono">{timeOfDay(deploy.created_at).slice(0, 5)}</span>
      <span className="h-state"><Status state={deploy.status} /></span>
      <div className="h-main">
        <span className="r-title">
          <span className="r-name">{deploy.repo}</span>
          <span className="r-node">{deploy.node_id}</span>
        </span>
        {firstCommit && (
          <span className="r-meta h-line">
            <span className="mono">{firstCommit.hash}</span>
            <span className="h-msg">{firstCommit.message}</span>
          </span>
        )}
        {hasMeta && (
          <span className="r-meta">
            {deploy.approved_by && <span>approved by {deploy.approved_by}</span>}
            {superseded && <span>{deploy.reject_reason}</span>}
            {detail && (
              <button type="button" className="link-btn" aria-expanded={showError} onClick={onToggleError}>
                {showError ? 'Hide' : 'Show'} {deploy.error ? 'error' : 'reason'}
              </button>
            )}
          </span>
        )}
        {showError && detail && <pre className="err-block">{detail}</pre>}
      </div>
      {deploy.duration_ms != null ? (
        <div className="r-metric">
          <span className="m-label">{deploy.status === 'deploying' ? 'running' : 'took'}</span>
          <span className="m-num">{durMs(deploy.duration_ms)}</span>
        </div>
      ) : (
        <div className="r-metric" />
      )}
    </div>
  );
}
