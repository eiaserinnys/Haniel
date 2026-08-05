import { useState, useCallback, useMemo } from 'react';
import { Icon } from '@/components/shared/Icon';
import { RejectModal } from '@/components/shared/RejectModal';
import { relTime, parseCommit, cn } from '@/lib/utils';
import type { Deploy } from '@/types';

interface PendingViewProps {
  deploys: Deploy[];
  onApprove: (deployId: string) => Promise<boolean>;
  onReject: (deployId: string, reason: string) => void;
  onApproveAll: (ids: string[]) => Promise<readonly string[]>;
}

export function PendingView({
  deploys,
  onApprove,
  onReject,
  onApproveAll,
}: PendingViewProps) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [rejectTarget, setRejectTarget] = useState<Deploy | null>(null);

  const selfUpdateDeploys = useMemo(
    () => deploys.filter(deploy => deploy.is_self_update),
    [deploys],
  );
  const regularDeploys = useMemo(
    () => deploys.filter(deploy => !deploy.is_self_update),
    [deploys],
  );
  const blockedNodeIds = useMemo(
    () => new Set(selfUpdateDeploys.map(deploy => deploy.node_id)),
    [selfUpdateDeploys],
  );

  // Deploying cards are display-only (server returns 409 if you try to
  // approve them again) — exclude them from selection-driven actions.
  const selectableDeploys = useMemo(
    () => deploys.filter(
      deploy => deploy.status === 'pending'
        && (deploy.is_self_update || !blockedNodeIds.has(deploy.node_id)),
    ),
    [blockedNodeIds, deploys],
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

  const handleApproveOne = useCallback(async (deploy: Deploy) => {
    if (!await onApprove(deploy.deploy_id)) return;
    const token = selectionTokens.get(deploy.deploy_id);
    if (!token) return;
    setSelected(current => {
      const next = new Set(current);
      next.delete(token);
      return next;
    });
  }, [onApprove, selectionTokens]);

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

  const pendingCount = deploys.filter(deploy => deploy.status === 'pending').length;
  const deployingCount = deploys.length - pendingCount;
  const blockedCount = regularDeploys.filter(
    deploy => deploy.status === 'pending' && blockedNodeIds.has(deploy.node_id),
  ).length;
  const subtitle =
    deployingCount > 0
      ? `${pendingCount} pending · ${deployingCount} deploying${blockedCount ? ` · ${blockedCount} waiting for self-update` : ''}`
      : `${pendingCount} change${pendingCount !== 1 ? 's' : ''} detected · awaiting approval`;

  return (
    <div className="view-pending">
      <h1>Pending Deploys</h1>
      <p className="view-subtitle">{subtitle}</p>

      <div className="pending-actions">
        <label className="select-all-label">
          <input
            type="checkbox"
            className="native-checkbox"
            checked={allSelected}
            disabled={selectableDeploys.length === 0}
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

      {selfUpdateDeploys.length > 0 && (
        <PendingSection
          title="Self-updates"
          description="Update Haniel first so later deployments use the newest runner."
          priority
          deploys={selfUpdateDeploys}
          blockedNodeIds={blockedNodeIds}
          activeSelected={activeSelected}
          expanded={expanded}
          onToggleSelect={toggleSelect}
          onToggleExpand={toggleExpand}
          onApprove={handleApproveOne}
          onReject={setRejectTarget}
        />
      )}

      {regularDeploys.length > 0 && (
        <PendingSection
          title="Repository updates"
          description={blockedCount > 0 ? `${blockedCount} update${blockedCount === 1 ? '' : 's'} waiting for Haniel on the same node.` : undefined}
          deploys={regularDeploys}
          blockedNodeIds={blockedNodeIds}
          activeSelected={activeSelected}
          expanded={expanded}
          onToggleSelect={toggleSelect}
          onToggleExpand={toggleExpand}
          onApprove={handleApproveOne}
          onReject={setRejectTarget}
        />
      )}

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

interface PendingSectionProps {
  title: string;
  description?: string;
  priority?: boolean;
  deploys: Deploy[];
  blockedNodeIds: Set<string>;
  activeSelected: Set<string>;
  expanded: Set<string>;
  onToggleSelect: (deploy: Deploy) => void;
  onToggleExpand: (id: string) => void;
  onApprove: (deploy: Deploy) => void;
  onReject: (deploy: Deploy) => void;
}

function PendingSection({
  title,
  description,
  priority = false,
  deploys,
  blockedNodeIds,
  activeSelected,
  expanded,
  onToggleSelect,
  onToggleExpand,
  onApprove,
  onReject,
}: PendingSectionProps) {
  const headingId = `pending-section-${title.toLowerCase().replaceAll(' ', '-')}`;

  return (
    <section className={cn('pending-section', priority && 'is-priority')} aria-labelledby={headingId}>
      <div className="pending-section-header">
        <div>
          <h2 id={headingId}>{title}</h2>
          {description && <p>{description}</p>}
        </div>
        <span className="pending-section-count">{deploys.length}</span>
      </div>
      <div className="pending-list">
        {deploys.map(deploy => {
          const approvalBlockedReason = !deploy.is_self_update && blockedNodeIds.has(deploy.node_id)
            ? 'Self-update required first'
            : undefined;
          return (
            <PendingCard
              key={deploy.deploy_id}
              deploy={deploy}
              isDeploying={deploy.status === 'deploying'}
              approvalBlockedReason={approvalBlockedReason}
              isSelected={activeSelected.has(deploy.deploy_id)}
              isExpanded={expanded.has(deploy.deploy_id)}
              onToggleSelect={() => onToggleSelect(deploy)}
              onToggleExpand={() => onToggleExpand(deploy.deploy_id)}
              onApprove={() => onApprove(deploy)}
              onReject={() => onReject(deploy)}
            />
          );
        })}
      </div>
    </section>
  );
}

/* ── PendingCard ─────────────────────────────────────── */

interface PendingCardProps {
  deploy: Deploy;
  isDeploying: boolean;
  approvalBlockedReason?: string;
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
  approvalBlockedReason,
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
        approvalBlockedReason && 'is-approval-blocked',
      )}
    >
      <div className="pending-card-header">
        <input
          type="checkbox"
          className="native-checkbox"
          checked={isSelected}
          disabled={isDeploying || Boolean(approvalBlockedReason)}
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
              <button className="btn-reject" onClick={onReject}>
                {deploy.is_self_update ? 'Postpone' : 'Reject'}
              </button>
              <button
                className="btn-approve"
                onClick={onApprove}
                disabled={Boolean(approvalBlockedReason)}
                title={approvalBlockedReason}
              >
                <Icon name="check" size={12} />
                Approve
              </button>
            </>
          )}
        </div>
      </div>

      {approvalBlockedReason && (
        <div className="approval-blocked-reason" role="note">
          {approvalBlockedReason}
        </div>
      )}

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
