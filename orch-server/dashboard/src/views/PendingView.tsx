import { useState, useCallback, useMemo } from 'react';
import { Icon } from '@/components/shared/Icon';
import { RejectModal } from '@/components/shared/RejectModal';
import { relTime, parseCommit, cn } from '@/lib/utils';
import type { Deploy } from '@/types';

interface PendingViewProps {
  deploys: Deploy[];
  latestFailure: Deploy | null;
  onApprove: (deployId: string) => void;
  onReject: (deployId: string, reason: string) => void;
  onApproveAll: (ids: string[]) => Promise<readonly string[]>;
}

export function PendingView({
  deploys,
  latestFailure,
  onApprove,
  onReject,
  onApproveAll,
}: PendingViewProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [rejectTarget, setRejectTarget] = useState<Deploy | null>(null);

  // Deploying cards are display-only (server returns 409 if you try to
  // approve them again) — exclude them from selection-driven actions.
  const selectableDeploys = useMemo(
    () => deploys.filter(d => d.status === 'pending'),
    [deploys],
  );
  const selectionTokens = useMemo(
    () => new Map(
      selectableDeploys.map(deploy => [
        deploy.deploy_id,
        `${deploy.deploy_id}\u0000${deploy.created_at}`,
      ]),
    ),
    [selectableDeploys],
  );
  const currentTokens = useMemo(
    () => new Set(selectionTokens.values()),
    [selectionTokens],
  );
  const [knownTokens, setKnownTokens] = useState(currentTokens);
  if (!sameSet(knownTokens, currentTokens)) {
    setKnownTokens(currentTokens);
    setSelected(current => new Set(Array.from(current).filter(token => currentTokens.has(token))));
  }
  const activeSelected = useMemo(
    () => new Set(
      Array.from(selectionTokens.entries())
        .filter(([, token]) => selected.has(token))
        .map(([id]) => id),
    ),
    [selected, selectionTokens],
  );
  const allSelected =
    selectableDeploys.length > 0 && activeSelected.size === selectableDeploys.length;

  const toggleSelect = useCallback((deploy: Deploy) => {
    const token = `${deploy.deploy_id}\u0000${deploy.created_at}`;
    setSelected(s => {
      const next = new Set(s);
      if (next.has(token)) next.delete(token); else next.add(token);
      return next;
    });
  }, []);

  const toggleSelectAll = useCallback(() => {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set(selectionTokens.values()));
  }, [allSelected, selectionTokens]);

  const toggleExpand = useCallback((id: string) => {
    setExpanded(s => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const handleApproveAll = useCallback(async () => {
    const accepted = await onApproveAll(Array.from(activeSelected));
    setSelected(current => {
      const next = new Set(current);
      accepted.forEach(id => {
        const token = selectionTokens.get(id);
        if (token) next.delete(token);
      });
      return next;
    });
  }, [activeSelected, onApproveAll, selectionTokens]);

  if (deploys.length === 0) {
    return (
      <div className="view-pending">
        <h1>Pending Deploys</h1>
        {latestFailure && <LatestFailure deploy={latestFailure} />}
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

  const pendingCount = selectableDeploys.length;
  const deployingCount = deploys.length - pendingCount;
  const subtitle =
    deployingCount > 0
      ? `${pendingCount} pending · ${deployingCount} deploying`
      : `${pendingCount} change${pendingCount !== 1 ? 's' : ''} detected · awaiting approval`;

  return (
    <div className="view-pending">
      <h1>Pending Deploys</h1>
      <p className="view-subtitle">{subtitle}</p>
      {latestFailure && <LatestFailure deploy={latestFailure} />}

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
        {activeSelected.size > 0 && (
          <button className="btn-approve-all" onClick={handleApproveAll}>
            <Icon name="check" size={12} />
            Approve {activeSelected.size} selected
          </button>
        )}
      </div>

      <div className="pending-list">
        {deploys.map(deploy => (
          <PendingCard
            key={deploy.deploy_id}
            deploy={deploy}
            isDeploying={deploy.status === 'deploying'}
            isSelected={activeSelected.has(deploy.deploy_id)}
            isExpanded={expanded.has(deploy.deploy_id)}
            onToggleSelect={() => toggleSelect(deploy)}
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

function sameSet(left: Set<string>, right: Set<string>): boolean {
  return left.size === right.size && Array.from(left).every(value => right.has(value));
}

function LatestFailure({ deploy }: { deploy: Deploy }) {
  return (
    <div className="pending-card" aria-label="Latest deploy failure">
      <div className="pending-card-header">
        <div className="pending-card-info">
          <div className="pending-card-title">
            <span className="pending-repo">Last deploy failed</span>
            <span className="pending-sep">·</span>
            <span className="pending-branch">{deploy.repo}</span>
            <span className="pending-sep">·</span>
            <span className="pending-node">{deploy.node_id}</span>
          </div>
          <div className="pending-card-meta">
            <span>{deploy.branch}</span>
            <span className="pending-sep">·</span>
            <span>{relTime(deploy.updated_at)}</span>
          </div>
          <pre className="history-error-block">
            {deploy.error || deploy.reject_reason || 'Unknown deploy failure'}
          </pre>
        </div>
      </div>
    </div>
  );
}

/* ── PendingCard ─────────────────────────────────────── */

interface PendingCardProps {
  deploy: Deploy;
  isDeploying: boolean;
  isSelected: boolean;
  isExpanded: boolean;
  onToggleSelect: () => void;
  onToggleExpand: () => void;
  onApprove: () => void;
  onReject: () => void;
}

function PendingCard({
  deploy,
  isDeploying,
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
  const diffStatLines = (deploy.diff_stat ?? '')
    .split(/\r?\n/)
    .map(line => line.trimEnd())
    .filter(Boolean);
  const diffStatText = diffStatLines.join('\n');
  const diffSummary = diffStatLines[diffStatLines.length - 1] ?? '';

  return (
    <div
      className={cn(
        'pending-card',
        isSelected && 'is-selected',
        isDeploying && 'is-deploying',
      )}
    >
      <div className="pending-card-header">
        <input
          type="checkbox"
          className="native-checkbox"
          checked={isSelected}
          disabled={isDeploying}
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
            {diffSummary && <><span className="pending-sep">·</span><span>{diffSummary}</span></>}
            <span className="pending-sep">·</span>
            <span>detected {relTime(deploy.detected_at)}</span>
          </div>
        </div>

        <div className="pending-card-actions">
          {isDeploying ? (
            <span className="deploying-pill" aria-live="polite">
              <Icon name="loader" size={12} />
              Deploying
            </span>
          ) : (
            <>
              <button className="btn-reject" onClick={onReject}>Reject</button>
              <button className="btn-approve" onClick={onApprove}>
                <Icon name="check" size={12} />
                Approve
              </button>
            </>
          )}
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

          {diffStatText && (
            <div className="diff-stat-block">
              <span className="diff-stat-label">Git changes</span>
              <pre className="diff-stat" aria-label="Git changes">{diffStatText}</pre>
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
