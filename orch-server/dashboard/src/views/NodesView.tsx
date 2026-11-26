import { useState, useMemo } from 'react';
import { Icon } from '@/components/shared/Icon';
import { RowToggle } from '@/components/shared/RowToggle';
import { Status } from '@/components/shared/Status';
import { Empty, LoadError, Skeleton } from '@/components/shared/LoadStates';
import { pollText, servingCommit, servingTime } from '@/lib/deployInfo';
import { absTime, relTime, uptimeStr } from '@/lib/utils';
import type { InFlightCommand, LoadState, NodeService, OrchestratorNode } from '@/types';

type ServiceCommand = (nodeId: string, serviceName: string, action: 'restart' | 'stop') => void;
type LookupInFlight = (nodeId: string, serviceName: string) => InFlightCommand | null;

interface NodesViewProps {
  nodes: OrchestratorNode[];
  load?: LoadState;
  onRetry?: () => void;
  onServiceCommand?: ServiceCommand;
  lookupInFlight?: LookupInFlight;
}

const READY: LoadState = { status: 'ready' };

export function NodesView({
  nodes,
  load = READY,
  onRetry = () => undefined,
  onServiceCommand,
  lookupInFlight,
}: NodesViewProps) {
  const [filter, setFilter] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const filtered = useMemo(() => {
    if (!filter.trim()) return nodes;
    const q = filter.trim().toLowerCase();
    return nodes.filter(n =>
      n.hostname.toLowerCase().includes(q) ||
      n.node_id.toLowerCase().includes(q) ||
      n.os.toLowerCase().includes(q)
    );
  }, [nodes, filter]);

  const toggleExpand = (id: string) => {
    setExpanded(s => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const showTools = load.status === 'ready' && nodes.length > 0;

  let body;
  if (load.status === 'loading') body = <Skeleton rows={3} />;
  else if (load.status === 'error') body = <LoadError what="nodes" detail={load.detail} onRetry={onRetry} />;
  else if (nodes.length === 0) {
    body = <div className="ledger"><Empty title="No nodes yet" text="Nodes appear when they connect." /></div>;
  } else {
    body = (
      <div className="ledger">
        {filtered.map(node => (
          <NodeRow
            key={node.node_id}
            node={node}
            isExpanded={expanded.has(node.node_id)}
            onToggleExpand={() => toggleExpand(node.node_id)}
            onServiceCommand={onServiceCommand}
            lookupInFlight={lookupInFlight}
          />
        ))}
        {filtered.length === 0 && <Empty inline text={`No nodes match “${filter.trim()}”`} />}
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <h1 className="page-title">Nodes</h1>
        {showTools && (
          <div className="page-tools">
            <label className="search">
              <span className="sr-only">Filter nodes</span>
              <Icon name="search" small />
              <input
                className="input"
                type="search"
                placeholder="Filter nodes"
                value={filter}
                onChange={e => setFilter(e.target.value)}
              />
            </label>
          </div>
        )}
      </div>
      {body}
    </>
  );
}

interface NodeRowProps {
  node: OrchestratorNode;
  isExpanded: boolean;
  onToggleExpand: () => void;
  onServiceCommand?: ServiceCommand;
  lookupInFlight?: LookupInFlight;
}

function NodeRow({ node, isExpanded, onToggleExpand, onServiceCommand, lookupInFlight }: NodeRowProps) {
  const isConnected = node.connected === 1;
  const restarting = !isConnected && Boolean(node.self_update_deploying);
  const status = isConnected ? 'connected' : restarting ? 'restarting' : 'disconnected';
  const serviceCount = node.services ? String(node.services.length) : '…';

  return (
    <div className="row no-lead" data-open={isExpanded ? '' : undefined}>
      <RowToggle open={isExpanded} onToggle={onToggleExpand}>
        <span className="r-title">
          <span className="r-name">{node.node_id}</span>
          <span className="r-branch">{node.os}</span>
        </span>
        <span className="r-meta">
          <Status state={status} />
          <span>haniel <span className="mono">{node.haniel_version}</span></span>
          {!isConnected && <span>last seen <span className="mono">{relTime(node.last_seen)}</span></span>}
        </span>
      </RowToggle>
      <div className="r-actions" />
      <div className="r-metric">
        <span className="m-label">services</span>
        <span className="m-num">{serviceCount}</span>
      </div>
      {isExpanded && (
        <div className="r-body">
          {isConnected ? (
            <NodeDetail node={node} onServiceCommand={onServiceCommand} lookupInFlight={lookupInFlight} />
          ) : (
            <div className="notice">
              <Status state={restarting ? 'restarting' : 'offline'} />
              <p className="notice-text">Last seen <span className="mono">{absTime(node.last_seen)}</span></p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function NodeDetail({ node, onServiceCommand, lookupInFlight }: Omit<NodeRowProps, 'isExpanded' | 'onToggleExpand'>) {
  const kv: [string, string, boolean][] = [
    ['Node ID', node.node_id, true],
    ['OS / Arch', `${node.os} / ${node.arch}`, false],
    ['Haniel', node.haniel_version, true],
    ['Last seen', relTime(node.last_seen), false],
  ];
  return (
    <>
      <div className="kv">
        {kv.map(([k, v, mono]) => (
          <div className="kv-item" key={k}>
            <span className="kv-k">{k}</span>
            <span className={mono ? 'kv-v mono' : 'kv-v'}>{v}</span>
          </div>
        ))}
      </div>
      <RepoLedger node={node} />
      <Services node={node} onServiceCommand={onServiceCommand} lookupInFlight={lookupInFlight} />
    </>
  );
}

interface RepoLine {
  repo: string;
  serving: { commit: string; at: string } | null;
  checked: string | null;
}

// One mini ledger for what the node serves (last successful deploy, from the
// hub's history) and when it last looked at each repository (from the node).
// A node too old to report its polling leaves those parts out.
function RepoLedger({ node }: { node: OrchestratorNode }) {
  const lines = new Map<string, RepoLine>();
  const line = (repo: string) => {
    const existing = lines.get(repo);
    if (existing) return existing;
    const created: RepoLine = { repo, serving: null, checked: null };
    lines.set(repo, created);
    return created;
  };
  for (const s of node.serving ?? []) {
    line(s.repo).serving = { commit: servingCommit(s), at: servingTime(s.deployed_at) };
  }
  for (const f of node.repo_fetches ?? []) {
    line(f.repo).checked = f.last_fetch;
  }
  const poll = node.poll_interval ?? null;
  const noneServed = (node.serving ?? []).length === 0;
  if (lines.size === 0 && poll === null && node.serving === undefined) return null;

  return (
    <div className="mini" role="group" aria-label="Repositories">
      <p className="mini-note">
        Last successful deploy per repository
        {poll !== null && <> · Polls every <span className="mono">{pollText(poll)}</span></>}
      </p>
      {noneServed && lines.size === 0 && <p className="mini-empty">No successful deploy recorded</p>}
      {lines.size > 0 && (
        <ul className="mini-rows">
          {Array.from(lines.values()).map(l => (
            <li key={l.repo}>
              <span className="r-name" title={l.repo}>{l.repo}</span>
              <span className="mini-serving">
                {l.serving
                  ? <>serving <span className="mono">{l.serving.commit}</span> · <span className="mono">{l.serving.at}</span></>
                  : 'No successful deploy recorded'}
              </span>
              <span className="mini-checked">
                {l.checked ? <>checked <span className="mono">{relTime(l.checked)}</span></> : null}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function serviceState(svc: NodeService): string {
  if (!svc.enabled) return 'disabled';
  // process_status=stopped takes precedence over health state — covers the
  // pid=None + health=ready race window (atom 260519.01).
  return svc.process_status === 'stopped' ? 'stopped' : svc.health_status;
}

function Services({ node, onServiceCommand, lookupInFlight }: Omit<NodeRowProps, 'isExpanded' | 'onToggleExpand'>) {
  const services = node.services;
  if (services === undefined) {
    return (
      <div>
        <table className="table" aria-busy="true">
          <thead>
            <tr>
              <th>Service</th><th className="num">Port</th><th className="num">PID</th>
              <th className="num">Uptime</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {[1, 2, 3].map(i => (
              <tr key={i} aria-hidden="true">
                <td><span className="sk w2" /></td>
                <td><span className="sk meta" /></td>
                <td><span className="sk meta" /></td>
                <td><span className="sk meta" /></td>
                <td><span className="sk meta w2" /></td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="empty-text">Waiting for service info from the node</p>
      </div>
    );
  }
  if (services.length === 0) return <p className="empty-text">No services configured on this node.</p>;

  return (
    <>
      <div className="svc-list">
        {services.map(svc => (
          <div className={svc.enabled ? 'svc-item' : 'svc-item is-dim'} key={svc.name}>
            <div className="svc-main">
              <span className="r-title">
                <span className="r-name" title={svc.name}>{svc.name}</span>
                <Status state={serviceState(svc)} />
              </span>
              {(svc.deps?.length ?? 0) > 0 && <span className="r-meta"><span>after {(svc.deps ?? []).join(', ')}</span></span>}
              <span className="r-meta">
                <span>Port <span className="mono">{svc.port ?? '—'}</span></span>
                <span>PID <span className="mono">{svc.pid ?? '—'}</span></span>
                <span>Uptime <span className="mono">{uptimeStr(svc.uptime_ms)}</span></span>
              </span>
            </div>
            <div className="svc-act">
              <ServiceActions nodeId={node.node_id} svc={svc} onServiceCommand={onServiceCommand} lookupInFlight={lookupInFlight} />
            </div>
          </div>
        ))}
      </div>
      <div className="table-wrap scroll scroll-x">
        <table className="table">
          <thead>
            <tr>
              <th>Service</th><th className="num">Port</th><th className="num">PID</th>
              <th className="num">Uptime</th><th>Role</th><th>Status</th>
              <th><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {services.map(svc => (
              <tr key={svc.name} className={svc.enabled ? undefined : 'is-dim'}>
                <td>
                  <span className="svc-name" title={svc.name}>{svc.name}</span>
                  {(svc.deps?.length ?? 0) > 0 && <span className="svc-deps">after {(svc.deps ?? []).join(', ')}</span>}
                </td>
                <td className="num mono">{svc.port ?? '—'}</td>
                <td className="num mono">{svc.pid ?? '—'}</td>
                <td className="num mono">{uptimeStr(svc.uptime_ms)}</td>
                <td>{svc.role || '—'}</td>
                <td><Status state={serviceState(svc)} /></td>
                <td className="act">
                  <ServiceActions nodeId={node.node_id} svc={svc} onServiceCommand={onServiceCommand} lookupInFlight={lookupInFlight} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

interface ServiceActionsProps {
  nodeId: string;
  svc: NodeService;
  onServiceCommand?: ServiceCommand;
  lookupInFlight?: LookupInFlight;
}

function ServiceActions({ nodeId, svc, onServiceCommand, lookupInFlight }: ServiceActionsProps) {
  if (!onServiceCommand || !svc.enabled) return null;
  // Action visibility is driven by process_status only — health state
  // (ready/starting/…) must not hide the buttons.
  const inFlight = lookupInFlight?.(nodeId, svc.name) ?? null;
  const busy = inFlight !== null;
  const progress = busy ? `${inFlight.action} in progress` : undefined;

  if (svc.process_status === 'running') {
    return (
      <span className="act-group">
        <button
          type="button"
          className="icon-btn"
          disabled={busy}
          title={progress ?? 'Restart'}
          aria-label={`Restart ${svc.name}`}
          onClick={() => onServiceCommand(nodeId, svc.name, 'restart')}
        >
          <Icon name={busy && inFlight.action === 'restart' ? 'loader' : 'restart'} className={busy && inFlight.action === 'restart' ? 'spin' : undefined} />
        </button>
        <button
          type="button"
          className="icon-btn"
          disabled={busy}
          title={progress ?? 'Stop'}
          aria-label={`Stop ${svc.name}`}
          onClick={() => onServiceCommand(nodeId, svc.name, 'stop')}
        >
          <Icon name={busy && inFlight.action === 'stop' ? 'loader' : 'stop'} className={busy && inFlight.action === 'stop' ? 'spin' : undefined} />
        </button>
      </span>
    );
  }
  return (
    <span className="act-group">
      <button
        type="button"
        className="icon-btn"
        disabled={busy}
        title={progress ?? 'Start'}
        aria-label={`Start ${svc.name}`}
        onClick={() => onServiceCommand(nodeId, svc.name, 'restart')}
      >
        <Icon name={busy ? 'loader' : 'play'} className={busy ? 'spin' : undefined} />
      </button>
    </span>
  );
}
