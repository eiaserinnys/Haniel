import { useState, useCallback, useMemo } from 'react';
import { Icon } from '@/components/shared/Icon';
import { RejectModal } from '@/components/shared/RejectModal';
import { RowToggle } from '@/components/shared/RowToggle';
import { Status } from '@/components/shared/Status';
import { Empty, LoadError, Skeleton } from '@/components/shared/LoadStates';
import { relTime, parseCommit, parseDiffSummary } from '@/lib/utils';
import type { Deploy, LoadState } from '@/types';

interface PendingViewProps {
  deploys: Deploy[];
  load?: LoadState;
  onRetry?: () => void;
  onApprove: (deployId: string) => Promise<boolean>;
  onReject: (deployId: string, reason: string) => void;
  onApproveAll: (ids: string[]) => Promise<readonly string[]>;
}

const READY: LoadState = { status: 'ready' };

export function PendingView({
  deploys,
  load = READY,
  onRetry = () => undefined,
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

  const header = (tools: boolean) => (
    <div className="page-head">
      <h1 className="page-title">Pending Deploys</h1>
      {tools && (
        <div className="page-tools">
          <label className="check-label">
            <input
              type="checkbox"
              className="check"
              checked={allSelected}
              disabled={selectableDeploys.length === 0}
              onChange={toggleSelectAll}
            />
            Select all
          </label>
          <button
            type="button"
            className="btn btn-primary"
            disabled={activeSelected.size === 0}
            onClick={handleApproveAll}
          >
            {activeSelected.size > 0 ? `Approve ${activeSelected.size} selected` : 'Approve selected'}
          </button>
        </div>
      )}
    </div>
  );

  if (load.status === 'loading') {
    return <>{header(false)}<Skeleton rows={4} /></>;
  }
  if (load.status === 'error') {
    return <>{header(false)}<LoadError what="pending deploys" detail={load.detail} onRetry={onRetry} /></>;
  }
  if (deploys.length === 0) {
    return (
      <>
        {header(false)}
        <div className="ledger"><Empty title="All clear" text="New changes show up here." /></div>
      </>
    );
  }

  const blockedCount = regularDeploys.filter(
    deploy => deploy.status === 'pending' && blockedNodeIds.has(deploy.node_id),
  ).length;

  return (
    <>
      {header(true)}

      {selfUpdateDeploys.length > 0 && (
        <PendingSection
          title="Self-updates"
          description="Same-node updates wait for these."
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
          description={blockedCount > 0 ? `${blockedCount} waiting for Haniel on the same node.` : undefined}
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
    </>
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
    <section className="sect" aria-labelledby={headingId}>
      <div className="sect-head">
        <h2 className="sect-title" id={headingId}>{title}</h2>
        {description && <p className="sect-note">{description}</p>}
      </div>
      <div className={priority ? 'ledger ledger-accent' : 'ledger'}>
        {deploys.map(deploy => (
          <PendingRow
            key={deploy.deploy_id}
            deploy={deploy}
            isDeploying={deploy.status === 'deploying'}
            isBlocked={!deploy.is_self_update && blockedNodeIds.has(deploy.node_id)}
            isSelected={activeSelected.has(deploy.deploy_id)}
            isExpanded={expanded.has(deploy.deploy_id)}
            onToggleSelect={() => onToggleSelect(deploy)}
            onToggleExpand={() => onToggleExpand(deploy.deploy_id)}
            onApprove={() => onApprove(deploy)}
            onReject={() => onReject(deploy)}
          />
        ))}
      </div>
    </section>
  );
}

interface PendingRowProps {
  deploy: Deploy;
  isDeploying: boolean;
  isBlocked: boolean;
  isSelected: boolean;
  isExpanded: boolean;
  onToggleSelect: () => void;
  onToggleExpand: () => void;
  onApprove: () => void;
  onReject: () => void;
}

function PendingRow({
  deploy,
  isDeploying,
  isBlocked,
  isSelected,
  isExpanded,
  onToggleSelect,
  onToggleExpand,
  onApprove,
  onReject,
}: PendingRowProps) {
  const [showAllCommits, setShowAllCommits] = useState(false);
  const commits = deploy.commits.map(parseCommit);
  const visibleCommits = showAllCommits ? commits : commits.slice(0, 3);
  const hiddenCount = commits.length - visibleCommits.length;
  const diffStatLines = (deploy.diff_stat ?? '')
    .split(/\r?\n/)
    .map(line => line.trimEnd())
    .filter(Boolean);
  const diffStatText = diffStatLines.join('\n');
  const diffSummary = parseDiffSummary(diffStatLines[diffStatLines.length - 1] ?? '');
  const blockedId = `blocked-${deploy.deploy_id}`;

  return (
    <div className="row" data-open={isExpanded ? '' : undefined}>
      <div className="r-lead">
        <input
          type="checkbox"
          className="check"
          aria-label={`Select ${deploy.repo} on ${deploy.node_id}`}
          checked={isSelected}
          disabled={isDeploying || isBlocked}
          onChange={onToggleSelect}
        />
      </div>
      <RowToggle open={isExpanded} onToggle={onToggleExpand}>
        <span className="r-title">
          <span className="r-name sel">{deploy.repo}</span>
          <span className="r-branch">{deploy.branch}</span>
        </span>
        <span className="r-meta">
          <span className="r-node">{deploy.node_id}</span>
          {diffSummary && (
            <span>{diffSummary.files} files <span className="mono">+{diffSummary.add} −{diffSummary.del}</span></span>
          )}
          <span className="mono">{relTime(deploy.detected_at)}</span>
          {isBlocked && !isDeploying && (
            <span className="r-blocked" id={blockedId}>
              <Icon name="lock" small />Waits for self-update
            </span>
          )}
        </span>
      </RowToggle>
      <div className="r-actions">
        {isDeploying ? (
          <Status state="deploying" />
        ) : (
          <>
            <button type="button" className="btn btn-outline" onClick={onReject}>
              {deploy.is_self_update ? 'Postpone' : 'Reject'}
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={onApprove}
              disabled={isBlocked}
              aria-describedby={isBlocked ? blockedId : undefined}
            >
              Approve
            </button>
          </>
        )}
      </div>
      <div className="r-metric">
        <span className="m-label">commits</span>
        <span className="m-num">{commits.length}</span>
      </div>
      {isExpanded && (
        <div className="r-body">
          <div className="dl">
            {deploy.affected_services.length > 0 && (
              <>
                <span className="dl-k">Services</span>
                <div className="dl-v chips">
                  {deploy.affected_services.map(svc => <span key={svc} className="tag">{svc}</span>)}
                </div>
              </>
            )}
            <span className="dl-k">Commits</span>
            <div className="dl-v">
              <ul className="commits">
                {visibleCommits.map((c, i) => (
                  <li key={i}>
                    <span className="commit-hash">{c.hash}</span>
                    <span className="commit-msg">{c.message}</span>
                  </li>
                ))}
              </ul>
              {hiddenCount > 0 && (
                <button type="button" className="link-btn" onClick={() => setShowAllCommits(true)}>
                  {hiddenCount} more commit{hiddenCount !== 1 ? 's' : ''}
                </button>
              )}
            </div>
            {diffStatText && (
              <>
                <span className="dl-k">Files</span>
                <div className="dl-v">
                  <pre className="files scroll scroll-x" aria-label="Git changes">{diffStatText}</pre>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
