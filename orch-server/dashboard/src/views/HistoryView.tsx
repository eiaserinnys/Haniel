import { useState, useMemo } from 'react';
import { Icon } from '@/components/shared/Icon';
import { StatusPill } from '@/components/shared/StatusPill';
import { timeOfDay, dateLabel, durMs, parseCommit, cn } from '@/lib/utils';
import type { Deploy, DeployStatus } from '@/types';

interface HistoryViewProps {
  deploys: Deploy[];
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

export function HistoryView({ deploys }: HistoryViewProps) {
  const [activeTab, setActiveTab] = useState<FilterTab>('all');
  const [expandedErrors, setExpandedErrors] = useState<Set<string>>(new Set());

  const counts = useMemo(() => {
    const c = { all: deploys.length, pending: 0, approved: 0, deploying: 0, success: 0, failed: 0, rejected: 0 };
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

  if (deploys.length === 0) {
    return (
      <div className="view-history">
        <h1>Deploy History</h1>
        <div className="empty-state">
          <div className="empty-icon">
            <Icon name="history" size={28} />
          </div>
          <h2 className="empty-title">No history</h2>
          <p className="empty-desc">Deploy events will appear here.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="view-history">
      <h1>Deploy History</h1>
      <p className="view-subtitle">
        {deploys.length} events · {counts.success} success · {counts.failed} failed · {counts.rejected} rejected
      </p>

      <div className="history-tabs">
        {TABS.map(tab => (
          <button
            key={tab.key}
            className={cn('history-tab', activeTab === tab.key && 'is-active')}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
            <span className="tab-count">{counts[tab.key]}</span>
          </button>
        ))}
      </div>

      {groups.length === 0 && (
        <div className="empty-state-inline">
          <p>No events match this filter.</p>
        </div>
      )}

      {groups.map(([label, items]) => (
        <div key={label} className="history-group">
          <div className="history-date-label">{label}</div>
          <div className="history-items">
            {items.map(item => (
              <HistoryItem
                key={item.deploy_id}
                deploy={item}
                showError={expandedErrors.has(item.deploy_id)}
                onToggleError={() => toggleError(item.deploy_id)}
              />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── HistoryItem ─────────────────────────────────────── */

interface HistoryItemProps {
  deploy: Deploy;
  showError: boolean;
  onToggleError: () => void;
}

function HistoryItem({ deploy, showError, onToggleError }: HistoryItemProps) {
  const firstCommit = deploy.commits.length > 0 ? parseCommit(deploy.commits[0]) : null;
  const hasError = deploy.error || deploy.reject_reason;

  return (
    <div className="history-item">
      <div className="history-rail">
        <span className={cn('history-dot', `dot-${deploy.status}`)} />
        <div className="history-rail-line" />
      </div>
      <div className="history-content">
        <div className="history-row-1">
          <span className="history-time">{timeOfDay(deploy.created_at)}</span>
          <StatusPill status={deploy.status} size="sm" />
          <span className="history-repo">{deploy.repo}</span>
          <span className="pending-sep">·</span>
          <span className="history-node">{deploy.node_id}</span>
          {firstCommit && (
            <>
              <span className="pending-sep">·</span>
              <code className="commit-hash">{firstCommit.hash}</code>
              <span className="history-commit-msg">{firstCommit.message}</span>
            </>
          )}
        </div>
        <div className="history-row-2">
          {deploy.approved_by && <span>approved by {deploy.approved_by}</span>}
          {deploy.duration_ms != null && (
            <span>{deploy.status === 'deploying' ? 'running' : 'took'} {durMs(deploy.duration_ms)}</span>
          )}
          {hasError && (
            <button className="history-error-btn" onClick={onToggleError}>
              {showError ? 'hide' : 'show'} {deploy.error ? 'error' : 'reason'}
            </button>
          )}
        </div>
        {showError && hasError && (
          <pre className="history-error-block">
            {deploy.error || deploy.reject_reason}
          </pre>
        )}
      </div>
    </div>
  );
}
