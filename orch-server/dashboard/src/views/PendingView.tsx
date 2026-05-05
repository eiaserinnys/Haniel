import { useState, useCallback } from 'react';
import { Icon } from '@/components/shared/Icon';
import { RejectModal } from '@/components/shared/RejectModal';
import { relTime, parseCommit, cn } from '@/lib/utils';
import type { Deploy } from '@/types';

interface PendingViewProps {
  deploys: Deploy[];
  onApprove: (deployId: string) => void;
  onReject: (deployId: string, reason: string) => void;
  onApproveAll: (ids: string[]) => void;
}

export function PendingView({ deploys, onApprove, onReject, onApproveAll }: PendingViewProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [rejectTarget, setRejectTarget] = useState<Deploy | null>(null);

  const allSelected = deploys.length > 0 && selected.size === deploys.length;

  const toggleSelect = useCallback((id: string) => {
    setSelected(s => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const toggleSelectAll = useCallback(() => {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set(deploys.map(d => d.deploy_id)));
  }, [allSelected, deploys]);

  const toggleExpand = useCallback((id: string) => {
    setExpanded(s => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const handleApproveAll = useCallback(() => {
    onApproveAll(Array.from(selected));
  }, [selected, onApproveAll]);

  if (deploys.length === 0) {
    return (
      <div className="view-pending">
        <h1>Pending Deploys</h1>
        <div className="empty-state">
          <div className="empty-icon">
            <Icon name="check" size={28} />
          </div>
          <h2 className="empty-title">All clear</h2>
          <p className="empty-desc">No pending deploys. Watching repos across connected nodes.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="view-pending">
      <h1>Pending Deploys</h1>
      <p className="view-subtitle">{deploys.length} changes detected · awaiting approval</p>

      <div className="pending-actions">
        <label className="select-all-label">
          <input
            type="checkbox"
            className="native-checkbox"
            checked={allSelected}
            onChange={toggleSelectAll}
          />
          <span className="select-all-text">Select all</span>
        </label>
        {selected.size > 0 && (
          <button className="btn-approve-all" onClick={handleApproveAll}>
            <Icon name="check" size={12} />
            Approve {selected.size} selected
          </button>
        )}
      </div>

      <div className="pending-list">
        {deploys.map(deploy => (
          <PendingCard
            key={deploy.deploy_id}
            deploy={deploy}
            isSelected={selected.has(deploy.deploy_id)}
            isExpanded={expanded.has(deploy.deploy_id)}
            onToggleSelect={() => toggleSelect(deploy.deploy_id)}
            onToggleExpand={() => toggleExpand(deploy.deploy_id)}
            onApprove={() => onApprove(deploy.deploy_id)}
            onReject={() => setRejectTarget(deploy)}
          />
        ))}
      </div>

      {rejectTarget && (
        <RejectModal
          deploy={rejectTarget}
          onConfirm={(reason) => { onReject(rejectTarget.deploy_id, reason); setRejectTarget(null); }}
          onClose={() => setRejectTarget(null)}
        />
      )}
    </div>
  );
}

/* ── PendingCard ─────────────────────────────────────── */

interface PendingCardProps {
  deploy: Deploy;
  isSelected: boolean;
  isExpanded: boolean;
  onToggleSelect: () => void;
  onToggleExpand: () => void;
  onApprove: () => void;
  onReject: () => void;
}

function PendingCard({
  deploy,
  isSelected,
  isExpanded,
  onToggleSelect,
  onToggleExpand,
  onApprove,
  onReject,
}: PendingCardProps) {
  const [showAllCommits, setShowAllCommits] = useState(false);
  const commits = deploy.commits.map(parseCommit);
  const visibleCommits = showAllCommits ? commits : commits.slice(0, 3);
  const hiddenCount = commits.length - 3;

  // Parse diff_stat: "+123 −45" or "3 files"
  const diffLabel = deploy.diff_stat || '';

  return (
    <div className={cn('pending-card', isSelected && 'is-selected')}>
      <div className="pending-card-header">
        <input
          type="checkbox"
          className="native-checkbox"
          checked={isSelected}
          onChange={onToggleSelect}
        />
        <button
          className={cn('expand-btn', isExpanded && 'is-expanded')}
          onClick={onToggleExpand}
          aria-label={isExpanded ? 'Collapse' : 'Expand'}
        >
          <Icon name="chevron" size={12} />
        </button>

        <div className="pending-card-info" onClick={onToggleExpand}>
          <div className="pending-card-title">
            <span className="pending-repo">{deploy.repo}</span>
            <span className="pending-sep">·</span>
            <span className="pending-branch">{deploy.branch}</span>
            <span className="pending-sep">·</span>
            <span className="pending-node">{deploy.node_id}</span>
          </div>
          <div className="pending-card-meta">
            <span>{commits.length} commit{commits.length !== 1 ? 's' : ''}</span>
            {diffLabel && <><span className="pending-sep">·</span><span>{diffLabel}</span></>}
            <span className="pending-sep">·</span>
            <span>detected {relTime(deploy.detected_at)}</span>
          </div>
        </div>

        <div className="pending-card-actions">
          <button className="btn-reject" onClick={onReject}>Reject</button>
          <button className="btn-approve" onClick={onApprove}>
            <Icon name="check" size={12} />
            Approve
          </button>
        </div>
      </div>

      {isExpanded && (
        <div className="pending-card-body">
          {deploy.affected_services.length > 0 && (
            <div className="affected-services">
              <span className="affected-label">Affected services</span>
              <div className="service-chips">
                {deploy.affected_services.map(svc => (
                  <span key={svc} className="service-chip">{svc}</span>
                ))}
              </div>
            </div>
          )}

          <div className="commit-timeline">
            {visibleCommits.map((c, i) => (
              <div key={i} className="commit-row">
                <div className="commit-rail">
                  <Icon name="commit" size={12} />
                  {i < visibleCommits.length - 1 && <div className="commit-rail-line" />}
                </div>
                <div className="commit-detail">
                  <code className="commit-hash">{c.hash}</code>
                  <span className="commit-msg">{c.message}</span>
                </div>
              </div>
            ))}
            {!showAllCommits && hiddenCount > 0 && (
              <button className="more-commits" onClick={() => setShowAllCommits(true)}>
                + {hiddenCount} more commit{hiddenCount !== 1 ? 's' : ''}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
