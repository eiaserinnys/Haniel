import { useState, useEffect, useCallback, useRef } from 'react';
import { Sidebar } from '@/components/layout/Sidebar';
import { Topbar } from '@/components/layout/Topbar';
import { MobileNav } from '@/components/layout/MobileNav';
import { Toaster } from '@/components/shared/Toaster';
import { PendingView } from '@/views/PendingView';
import { NodesView } from '@/views/NodesView';
import { HistoryView } from '@/views/HistoryView';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useWebSocket } from '@/hooks/useWebSocket';
import {
  useInFlightCommands,
  SERVICE_SPINNER_MIN_MS,
} from '@/hooks/useInFlightCommands';
import * as api from '@/lib/api';
import { cn } from '@/lib/utils';
import type {
  Page,
  Deploy,
  OrchestratorNode,
  Toast,
  WsEvent,
  ApproveResponse,
} from '@/types';

function App() {
  const [page, setPageRaw] = useState<Page>('pending');
  const [pending, setPending] = useState<Deploy[]>([]);
  const [nodes, setNodes] = useState<OrchestratorNode[]>([]);
  const [history, setHistory] = useState<Deploy[]>([]);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const {
    inFlight,
    add: addInFlight,
    removeWithMinDelay,
    clear: clearInFlight,
    lookupByService,
  } = useInFlightCommands();
  const [theme, setTheme] = useState<'dark' | 'light'>(() => {
    const stored = localStorage.getItem('haniel-theme');
    return (stored === 'light' || stored === 'dark') ? stored : 'dark';
  });
  const [showSidebar, setShowSidebar] = useState(true);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const isMobile = useIsMobile();

  // Extract token from URL (set by OAuth callback redirect) and store in localStorage
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');
    if (token) {
      localStorage.setItem('haniel-token', token);
      // Remove token from URL without reload
      window.history.replaceState({}, '', window.location.pathname);
    }
  }, []);

  // Toast helper
  const pushToast = useCallback((text: string, kind: Toast['kind'] = 'info') => {
    const id = Math.random().toString(36).slice(2);
    setToasts(ts => [...ts, { id, text, kind }]);
    setTimeout(() => setToasts(ts => ts.filter(x => x.id !== id)), 4200);
  }, []);

  // WebSocket event handler
  const handleWsEvent = useCallback((event: WsEvent) => {
    switch (event.type) {
      case 'new_pending':
        // Refetch full pending list (event doesn't include commits/services)
        api.fetchPending().then(r => setPending(r.deploys)).catch(() => {});
        pushToast(`New pending: ${event.repo} on ${event.node_id}`, 'amber');
        break;
      case 'status_change':
        // Pending and deploying are both shown in PendingView — keep the
        // card visible. Refetch the active list so its status (and any
        // newly attached fields) is in sync with the server.
        if (event.status === 'pending' || event.status === 'deploying') {
          api.fetchPending().then(r => setPending(r.deploys)).catch(() => {});
        } else {
          // Terminal — remove from PendingView. HistoryView refetch picks it up.
          setPending(ps => ps.filter(p => p.deploy_id !== event.deploy_id));
        }
        // Refetch history to get latest
        api.fetchHistory().then(r => setHistory(r.deploys)).catch(() => {});
        if (event.status === 'deploying') {
          pushToast(`Deploying ${event.deploy_id.split(':')[1] || ''}`, 'amber');
        } else if (event.status === 'success') {
          pushToast(`Deploy succeeded`, 'success');
        } else if (event.status === 'failed') {
          pushToast(
            `Deploy failed${event.reject_reason ? `: ${event.reject_reason}` : ''}`,
            'error',
          );
        } else if (
          event.status === 'rejected' &&
          event.reject_reason &&
          event.reject_reason.startsWith('superseded')
        ) {
          // Auto-supersede triggered by approving a newer deploy on the
          // same (node, repo, branch). User-driven rejects already toast
          // from handleReject.
          pushToast(`Superseded: newer deploy queued`, 'amber');
        }
        break;
      case 'node_connected':
        // Refetch nodes
        api.fetchNodes().then(r => setNodes(r.nodes)).catch(() => {});
        pushToast(`Node connected: ${event.hostname}`, 'info');
        break;
      case 'node_disconnected':
        api.fetchNodes().then(r => setNodes(r.nodes)).catch(() => {});
        pushToast(`Node disconnected: ${event.node_id}`, 'amber');
        break;
      case 'service_command_result':
        // Release the matching button regardless of success/failure, but
        // keep the spinner visible for at least SERVICE_SPINNER_MIN_MS so
        // the user perceives that something happened on a fast response.
        removeWithMinDelay(event.command_id, SERVICE_SPINNER_MIN_MS);
        if (event.success) {
          pushToast(`${event.action} ${event.service_name}: success`, 'success');
        } else {
          // error includes 'timeout', 'node disconnected', or node-reported error.
          pushToast(`${event.action} ${event.service_name}: ${event.error || 'failed'}`, 'error');
        }
        // Refetch nodes to update service status
        api.fetchNodes().then(r => setNodes(r.nodes)).catch(() => {});
        break;
    }
  }, [pushToast, removeWithMinDelay]);

  const { status: wsStatus } = useWebSocket(handleWsEvent);

  // WS disconnected (final, after MAX_RETRIES) → clear in-flight commands so
  // buttons unlock. The server-side 30s timeout would eventually broadcast
  // a 'timeout' result, but the dashboard wouldn't be listening anymore.
  // Read inFlight via ref so this effect only runs on wsStatus changes —
  // including inFlight.size in deps would let the previous status update
  // before a real disconnected transition is observed.
  const prevWsStatus = useRef(wsStatus);
  const inFlightRef = useRef(inFlight);
  inFlightRef.current = inFlight;
  useEffect(() => {
    if (prevWsStatus.current !== 'disconnected' && wsStatus === 'disconnected') {
      const n = inFlightRef.current.size;
      if (n > 0) {
        clearInFlight();
        pushToast(`Lost connection — ${n} in-flight command(s) cleared`, 'amber');
      }
    }
    prevWsStatus.current = wsStatus;
  }, [wsStatus, clearInFlight, pushToast]);

  // Auto-close mobile drawer when crossing to desktop
  useEffect(() => { if (!isMobile) setMobileMenuOpen(false); }, [isMobile]);

  // Apply theme to <html>
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('haniel-theme', theme);
  }, [theme]);

  // Initial data fetch
  useEffect(() => {
    api.fetchPending().then(r => setPending(r.deploys)).catch(() => {});
    api.fetchNodes().then(r => setNodes(r.nodes)).catch(() => {});
    api.fetchHistory().then(r => setHistory(r.deploys)).catch(() => {});
  }, []);

  // Page navigation
  const setPage = useCallback((p: Page) => { setPageRaw(p); setMobileMenuOpen(false); }, []);

  // Action handlers
  const handleApprove = useCallback(async (deployId: string) => {
    try {
      const res = await api.approveDeploy(deployId);
      // Don't filter setPending here. The status_change WS event handles
      // visibility: 'deploying' keeps the card visible, terminal states
      // (or 'approved' with offline node) remove it.
      if (res.warning) {
        // Server returned 200 but the node was offline — surface the warning
        // instead of pretending the deploy started.
        pushToast(`Approved (deferred): ${res.warning}`, 'amber');
      } else {
        pushToast(`Approved deploy`, 'success');
      }
    } catch (e) {
      pushToast(`Approve failed: ${e instanceof api.ApiError ? e.body : 'Unknown error'}`, 'error');
    }
  }, [pushToast]);

  const handleReject = useCallback(async (deployId: string, reason: string) => {
    try {
      await api.rejectDeploy(deployId, reason);
      setPending(ps => ps.filter(p => p.deploy_id !== deployId));
      pushToast(`Rejected deploy`, 'info');
    } catch (e) {
      pushToast(`Reject failed: ${e instanceof api.ApiError ? e.body : 'Unknown error'}`, 'error');
    }
  }, [pushToast]);

  const handleServiceCommand = useCallback(async (nodeId: string, serviceName: string, action: 'restart' | 'stop') => {
    try {
      const res = await api.serviceCommand(nodeId, serviceName, action);
      // Buttons disable until the matching service_command_result arrives
      // (or the WS finalises as 'disconnected', which clears in-flight).
      addInFlight({
        commandId: res.command_id,
        nodeId,
        serviceName,
        action,
      });
      pushToast(`${action} ${serviceName} sent`, 'info');
    } catch (e) {
      // 503 (node not connected), 400, etc. — no in-flight added, button stays idle.
      pushToast(`${action} failed: ${e instanceof api.ApiError ? e.body : 'Unknown error'}`, 'error');
    }
  }, [pushToast, addInFlight]);

  const handleApproveAll = useCallback(async (ids: string[]) => {
    if (ids.length > 0) {
      // Approve selected items individually. Classify each result as
      // succeeded / deferred (200 + warning) / failed (HTTP error).
      const results = await Promise.allSettled(ids.map(id => api.approveDeploy(id)));
      const fulfilled = results.filter(
        (r): r is PromiseFulfilledResult<ApproveResponse> => r.status === 'fulfilled',
      );
      const deferred = fulfilled.filter(r => !!r.value.warning).length;
      const succeeded = fulfilled.length - deferred;
      const failed = results.length - fulfilled.length;
      // Don't filter setPending here — the status_change WS events drive
      // visibility (deploying → keep, terminal → remove, failed approve →
      // stays as pending so the user can retry).
      if (failed === 0 && deferred === 0) {
        pushToast(`Approved ${succeeded} deploys`, 'success');
      } else if (failed === 0) {
        pushToast(`Approved ${succeeded}, deferred ${deferred} (node not connected)`, 'amber');
      } else {
        const firstRejected = results.find(
          (r): r is PromiseRejectedResult => r.status === 'rejected',
        );
        const firstFailureReason = firstRejected
          ? (firstRejected.reason instanceof api.ApiError
              ? firstRejected.reason.body
              : String(firstRejected.reason))
          : '';
        const tail = firstFailureReason ? ` (first failure: ${firstFailureReason})` : '';
        pushToast(`Approved ${succeeded}, deferred ${deferred}, failed ${failed}${tail}`, 'amber');
      }
    } else {
      // Approve all via server API
      try {
        const result = await api.approveAll();
        // Don't clear setPending — status_change events handle visibility.
        if (result.message === 'no pending deploys') {
          pushToast('No pending deploys', 'info');
        } else if (result.failed.length === 0) {
          pushToast(`Approved ${result.approved.length} deploys`, 'success');
        } else {
          const reasons = result.failed.map(f => f.reason).join(', ');
          pushToast(
            `Approved ${result.approved.length}, failed ${result.failed.length} (${reasons})`,
            'amber',
          );
        }
      } catch (e) {
        pushToast(`Approve all failed: ${e instanceof api.ApiError ? e.body : 'Unknown error'}`, 'error');
      }
    }
  }, [pushToast]);

  // Toggle theme
  const toggleTheme = useCallback(() => {
    setTheme(t => t === 'dark' ? 'light' : 'dark');
  }, []);

  // Menu toggle
  const handleMenuToggle = useCallback(() => {
    if (isMobile) setMobileMenuOpen(o => !o);
    else setShowSidebar(s => !s);
  }, [isMobile]);

  const nodesConnected = nodes.filter(n => n.connected === 1).length;

  return (
    <div className={cn('app', mobileMenuOpen && 'is-drawer-open')}>
      {(isMobile ? mobileMenuOpen : showSidebar) && (
        <Sidebar
          page={page}
          setPage={setPage}
          pendingCount={pending.length}
          nodesConnected={nodesConnected}
          historyCount={history.length}
          wsStatus={wsStatus}
        />
      )}

      {isMobile && mobileMenuOpen && (
        <button className="sidebar-scrim" aria-label="Close menu" onClick={() => setMobileMenuOpen(false)} />
      )}

      <div className="main">
        <Topbar
          page={page}
          pendingCount={pending.length}
          theme={theme}
          onToggleTheme={toggleTheme}
          onMenuToggle={handleMenuToggle}
        />

        <main className="page">
          {page === 'pending' && (
            <PendingView
              deploys={pending}
              onApprove={handleApprove}
              onReject={handleReject}
              onApproveAll={handleApproveAll}
            />
          )}
          {page === 'nodes' && (
            <NodesView
              nodes={nodes}
              onServiceCommand={handleServiceCommand}
              lookupInFlight={lookupByService}
            />
          )}
          {page === 'history' && <HistoryView deploys={history} />}
        </main>
      </div>

      <MobileNav page={page} setPage={setPage} pendingCount={pending.length} />
      <Toaster toasts={toasts} />
    </div>
  );
}

export default App;
