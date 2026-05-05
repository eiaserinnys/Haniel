import { useState, useMemo } from 'react';
import { Icon } from '@/components/shared/Icon';
import { StatusPill } from '@/components/shared/StatusPill';
import { relTime, uptimeStr, cn } from '@/lib/utils';
import type { OrchestratorNode } from '@/types';

interface NodesViewProps {
  nodes: OrchestratorNode[];
  onServiceCommand?: (nodeId: string, serviceName: string, action: 'restart' | 'stop') => void;
}

export function NodesView({ nodes, onServiceCommand }: NodesViewProps) {
  const [filter, setFilter] = useState('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const connected = nodes.filter(n => n.connected === 1).length;

  const filtered = useMemo(() => {
    if (!filter.trim()) return nodes;
    const q = filter.toLowerCase();
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

  if (nodes.length === 0) {
    return (
      <div className="view-nodes">
        <h1>Nodes</h1>
        <div className="empty-state">
          <div className="empty-icon">
            <Icon name="wifi-off" size={28} />
          </div>
          <h2 className="empty-title">No nodes</h2>
          <p className="empty-desc">No orchestrator nodes have connected yet.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="view-nodes">
      <h1>Nodes</h1>
      <p className="view-subtitle">
        {connected} connected · {nodes.length} total
      </p>

      <div className="nodes-toolbar">
        <div className="filter-input-wrap">
          <Icon name="search" size={13} />
          <input
            type="text"
            className="filter-input"
            placeholder="Filter nodes..."
            value={filter}
            onChange={e => setFilter(e.target.value)}
          />
        </div>
      </div>

      <div className="nodes-list">
        {filtered.map(node => (
          <NodeCard
            key={node.node_id}
            node={node}
            isExpanded={expanded.has(node.node_id)}
            onToggleExpand={() => toggleExpand(node.node_id)}
            onServiceCommand={onServiceCommand}
          />
        ))}
        {filtered.length === 0 && (
          <div className="empty-state-inline">
            <p>No nodes match "{filter}"</p>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── NodeCard ────────────────────────────────────────── */

interface NodeCardProps {
  node: OrchestratorNode;
  isExpanded: boolean;
  onToggleExpand: () => void;
  onServiceCommand?: (nodeId: string, serviceName: string, action: 'restart' | 'stop') => void;
}

function NodeCard({ node, isExpanded, onToggleExpand, onServiceCommand }: NodeCardProps) {
  const isConnected = node.connected === 1;
  const osIcon = node.os.toLowerCase().includes('windows') ? 'windows' : 'linux';

  return (
    <div className={cn('node-card', !isConnected && 'is-disconnected')}>
      <div className="node-card-header" onClick={onToggleExpand}>
        <span className={cn('node-led', isConnected && 'is-on')} />

        <div className="node-card-info">
          <div className="node-card-title">
            <span className="node-name">{node.node_id}</span>
            <span className="node-tag os-tag">
              <Icon name={osIcon} size={11} />
              {node.os}
            </span>
          </div>
          <div className="node-card-meta">
            <span>haniel {node.haniel_version}</span>
            <span className="pending-sep">·</span>
            {isConnected ? (
              <span style={{ color: 'var(--green)' }}>connected</span>
            ) : (
              <>
                <span style={{ color: 'var(--red)' }}>disconnected</span>
                <span className="pending-sep">·</span>
                <span>last seen {relTime(node.last_seen)}</span>
              </>
            )}
          </div>
        </div>

        <button
          className={cn('expand-btn', isExpanded && 'is-expanded')}
          aria-label={isExpanded ? 'Collapse' : 'Expand'}
        >
          <Icon name="chevron" size={12} />
        </button>
      </div>

      {isExpanded && (
        <div className="node-card-body">
          {!isConnected ? (
            <div className="node-offline-banner">
              <Icon name="wifi-off" size={14} />
              <span>This node was last seen {relTime(node.last_seen)}</span>
            </div>
          ) : (
            <div className="node-detail-grid">
              <div className="node-detail-row">
                <span className="node-detail-label">Node ID</span>
                <code className="node-detail-value">{node.node_id}</code>
              </div>
              <div className="node-detail-row">
                <span className="node-detail-label">OS / Arch</span>
                <span className="node-detail-value">{node.os} / {node.arch}</span>
              </div>
              <div className="node-detail-row">
                <span className="node-detail-label">Haniel</span>
                <span className="node-detail-value">{node.haniel_version}</span>
              </div>
              <div className="node-detail-row">
                <span className="node-detail-label">Last seen</span>
                <span className="node-detail-value">{relTime(node.last_seen)}</span>
              </div>
              {node.services && node.services.length > 0 && (
                <div className="node-services">
                  <table className="services-table">
                    <thead>
                      <tr>
                        <th>Service</th>
                        <th>Port</th>
                        <th>PID</th>
                        <th>Uptime</th>
                        <th className="hide-mobile">Role</th>
                        <th>Status</th>
                        {onServiceCommand && <th>Actions</th>}
                      </tr>
                    </thead>
                    <tbody>
                      {node.services.map(svc => (
                        <tr key={svc.name} className={cn(!svc.enabled && 'svc-dim')}>
                          <td className="svc-name">
                            {svc.name}
                            {svc.deps && svc.deps.length > 0 && (
                              <span className="svc-deps">{'\u2190'} {svc.deps.join(', ')}</span>
                            )}
                          </td>
                          <td>{svc.port ?? '\u2014'}</td>
                          <td>{svc.pid ?? '\u2014'}</td>
                          <td>{uptimeStr(svc.uptime_ms)}</td>
                          <td className="hide-mobile">{svc.role || '\u2014'}</td>
                          <td><StatusPill status={svc.status} size="sm" /></td>
                          {onServiceCommand && (
                            <td className="svc-actions">
                              {svc.enabled && svc.status === 'running' && (
                                <>
                                  <button className="svc-btn" onClick={() => onServiceCommand(node.node_id, svc.name, 'restart')} title="Restart">
                                    <Icon name="refresh" size={12} />
                                  </button>
                                  <button className="svc-btn svc-btn-danger" onClick={() => onServiceCommand(node.node_id, svc.name, 'stop')} title="Stop">
                                    <Icon name="stop" size={12} />
                                  </button>
                                </>
                              )}
                              {svc.enabled && svc.status === 'stopped' && (
                                <button className="svc-btn" onClick={() => onServiceCommand(node.node_id, svc.name, 'restart')} title="Start">
                                  <Icon name="play" size={12} />
                                </button>
                              )}
                            </td>
                          )}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
