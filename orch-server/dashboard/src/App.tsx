import { useState, useEffect, useCallback, useRef } from 'react';
import { Sidebar } from '@/components/layout/Sidebar';
import { Topbar } from '@/components/layout/Topbar';
import { MobileNav } from '@/components/layout/MobileNav';
import { Drawer } from '@/components/layout/Drawer';
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
import { useTheme } from '@/lib/theme';
import type {
  LoadState,
  Page,
  Deploy,
  OrchestratorNode,
  Toast,
  WsEvent,
  ApproveResponse,
} from '@/types';

const DASHBOARD_SYNC_INTERVAL_MS = 10_000;
const LOADING: LoadState = { status: 'loading' };

function describeFailure(path: string, e: unknown): string {
  if (e instanceof api.ApiError) return `GET ${path} → ${e.status}`;
  return `GET ${path} → ${e instanceof Error ? e.message : 'request failed'}`;
}

// A failed refresh only replaces the screen while nothing has loaded yet;
// once data is on screen, the next sync repairs it.
function failedLoad(current: LoadState, path: string, e: unknown): LoadState {
  return current.status === 'ready' ? current : { status: 'error', detail: describeFailure(path, e) };
}

function App() {
  const [page, setPageRaw] = useState<Page>('pending');
  const [pending, setPending] = useState<Deploy[]>([]);
  const [nodes, setNodes] = useState<OrchestratorNode[]>([]);
  const [history, setHistory] = useState<Deploy[]>([]);
  const [pendingLoad, setPendingLoad] = useState<LoadState>(LOADING);
  const [historyLoad, setHistoryLoad] = useState<LoadState>(LOADING);
  const [nodesLoad, setNodesLoad] = useState<LoadState>(LOADING);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const {
    inFlight,
    add: addInFlight,
    removeWithMinDelay,
    clear: clearInFlight,
    lookupByService,
  } = useInFlightCommands();
  const [theme, setTheme] = useTheme();
  const [showSidebar, setShowSidebar] = useState(true);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  // History 'include superseded' toggle — default off. Same value drives both
  // HistoryView (UI) and the fetchHistory query param so the response stays
  // consistent with the checkbox. Plain useState (no localStorage) so a fresh
  // session lands on the actionable view by default.
  const [includeSupersededHistory, setIncludeSupersededHistory] = useState(false);
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
  const dismissToast = useCallback((id: string) => {
    setToasts(ts => ts.filter(x => x.id !== id));
  }, []);

  const pushToast = useCallback((text: string, kind: Toast['kind']) => {
    const id = Math.random().toString(36).slice(2);
    setToasts(ts => [...ts, { id, text, kind }]);
    setTimeout(() => dismissToast(id), 4200);
  }, [dismissToast]);

  const deploySyncSeq = useRef(0);
  const nodeSyncSeq = useRef(0);

  const refreshDeploys = useCallback(() => {
    const seq = ++deploySyncSeq.current;
    void Promise.allSettled([
      api.fetchPending(),
      api.fetchHistory({ includeSuperseded: includeSupersededHistory }),
    ]).then(([pendingResult, historyResult]) => {
      if (seq !== deploySyncSeq.current) return;
      if (pendingResult.status === 'fulfilled') {
        setPending(pendingResult.value.deploys);
        setPendingLoad({ status: 'ready' });
      } else {
        setPendingLoad(cur => failedLoad(cur, '/api/orch/pending', pendingResult.reason));
      }
      if (historyResult.status === 'fulfilled') {
        setHistory(historyResult.value.deploys);
        setHistoryLoad({ status: 'ready' });
      } else {
        setHistoryLoad(cur => failedLoad(cur, '/api/orch/history', historyResult.reason));
      }
    });
  }, [includeSupersededHistory]);

  const refreshNodes = useCallback(() => {
    const seq = ++nodeSyncSeq.current;
    void api.fetchNodes()
      .then(r => {
        if (seq !== nodeSyncSeq.current) return;
        setNodes(r.nodes);
        setNodesLoad({ status: 'ready' });
      })
      .catch((e: unknown) => {
        if (seq === nodeSyncSeq.current) setNodesLoad(cur => failedLoad(cur, '/api/orch/nodes', e));
      });
  }, []);

  const retryDeploys = useCallback(() => {
    setPendingLoad(LOADING);
    setHistoryLoad(LOADING);
    refreshDeploys();
  }, [refreshDeploys]);

  const retryNodes = useCallback(() => {
    setNodesLoad(LOADING);
    refreshNodes();
  }, [refreshNodes]);

  const refreshAll = useCallback(() => {
    refreshDeploys();
    refreshNodes();
  }, [refreshDeploys, refreshNodes]);

  // WebSocket event handler
  const handleWsEvent = useCallback((event: WsEvent) => {
    switch (event.type) {
      case 'new_pending':
        // Refetch full deploy lists (event doesn't include commits/services).
        refreshDeploys();
        pushToast(`New pending: ${event.repo} on ${event.node_id}`, 'warn');
        break;
      case 'status_change':
        // Pending and deploying are both shown in PendingView — keep the
        // card visible. Refetch the active list so its status (and any
        // newly attached fields) is in sync with the server.
        if (event.status !== 'pending' && event.status !== 'deploying') {
          // Terminal — remove immediately; refreshDeploys reconciles with server state.
          setPending(ps => ps.filter(p => p.deploy_id !== event.deploy_id));
        }
        refreshDeploys();
        if (event.status === 'deploying') {
          pushToast(`Deploying ${event.deploy_id.split(':')[1] || ''}`, 'busy');
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
          // Match the server-side SQL filter exactly (LIKE 'superseded by %').
          // Trailing space prevents false matches like "superseded for testing".
          event.reject_reason.startsWith('superseded by ')
        ) {
          // Auto-supersede triggered by approving a newer deploy on the
          // same (node, repo, branch). User-driven rejects already toast
          // from handleReject.
          pushToast(`Superseded: newer deploy queued`, 'warn');
        }
        break;
      case 'node_connected':
        refreshNodes();
        pushToast(`Node connected: ${event.hostname}`, 'success');
        break;
      case 'node_disconnected':
        refreshNodes();
        refreshDeploys();
        pushToast(`Node disconnected: ${event.node_id}`, 'warn');
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
        refreshNodes();
        break;
    }
  }, [pushToast, refreshDeploys, refreshNodes, removeWithMinDelay]);

  const { status: wsStatus } = useWebSocket(handleWsEvent);

  // WS disconnected (final, after MAX_RETRIES) → clear in-flight commands so
  // buttons unlock. The server-side 30s timeout would eventually broadcast
  // a 'timeout' result, but the dashboard wouldn't be listening anymore.
  // Read inFlight via ref so this effect only runs on wsStatus changes —
  // including inFlight.size in deps would let the previous status update
  // before a real disconnected transition is observed.
  const prevWsStatus = useRef(wsStatus);
  const inFlightRef = useRef(inFlight);
  useEffect(() => {
    inFlightRef.current = inFlight;
  }, [inFlight]);

  useEffect(() => {
    if (prevWsStatus.current !== 'connected' && wsStatus === 'connected') {
      refreshAll();
    }
    if (prevWsStatus.current !== 'disconnected' && wsStatus === 'disconnected') {
      const n = inFlightRef.current.size;
      if (n > 0) {
        clearInFlight();
        pushToast(`Lost connection — ${n} in-flight command(s) cleared`, 'warn');
      }
    }
    prevWsStatus.current = wsStatus;
  }, [wsStatus, clearInFlight, pushToast, refreshAll]);

  // Initial data fetch, and history refetch when the superseded toggle changes.
  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  // WebSocket is the fast path; polling and focus refresh are the repair path
  // for events missed during reconnects, background tabs, or another dashboard's action.
  useEffect(() => {
    const syncIfVisible = () => {
      if (document.visibilityState === 'visible') refreshAll();
    };
    const intervalId = window.setInterval(syncIfVisible, DASHBOARD_SYNC_INTERVAL_MS);
    window.addEventListener('focus', refreshAll);
    document.addEventListener('visibilitychange', syncIfVisible);

    return () => {
      window.clearInterval(intervalId);
      window.removeEventListener('focus', refreshAll);
      document.removeEventListener('visibilitychange', syncIfVisible);
    };
  }, [refreshAll]);

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
        pushToast(`Approved (deferred): ${res.warning}`, 'warn');
      } else {
        pushToast(`Approved deploy`, 'success');
      }
      return true;
    } catch (e) {
      pushToast(`Approve failed: ${e instanceof api.ApiError ? e.body : 'Unknown error'}`, 'error');
      return false;
    } finally {
      refreshDeploys();
    }
  }, [pushToast, refreshDeploys]);

  const handleReject = useCallback(async (deployId: string, reason: string) => {
    try {
      await api.rejectDeploy(deployId, reason);
      setPending(ps => ps.filter(p => p.deploy_id !== deployId));
      pushToast(`Rejected deploy`, 'neutral');
    } catch (e) {
      pushToast(`Reject failed: ${e instanceof api.ApiError ? e.body : 'Unknown error'}`, 'error');
    } finally {
      refreshDeploys();
    }
  }, [pushToast, refreshDeploys]);

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
      pushToast(`${action} ${serviceName} sent`, 'busy');
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
      const acceptedIds = ids.filter((_, index) => results[index].status === 'fulfilled');
      const deferred = fulfilled.filter(r => !!r.value.warning).length;
      const succeeded = fulfilled.length - deferred;
      const failed = results.length - fulfilled.length;
      // Don't filter setPending here — the status_change WS events drive
      // visibility (deploying → keep, terminal → remove, failed approve →
      // stays as pending so the user can retry).
      if (failed === 0 && deferred === 0) {
        pushToast(`Approved ${succeeded} deploys`, 'success');
      } else if (failed === 0) {
        pushToast(`Approved ${succeeded}, deferred ${deferred} (node not connected)`, 'warn');
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
        pushToast(`Approved ${succeeded}, deferred ${deferred}, failed ${failed}${tail}`, 'warn');
      }
      refreshDeploys();
      return acceptedIds;
    } else {
      // Approve all via server API
      try {
        const result = await api.approveAll();
        // Don't clear setPending — status_change events handle visibility.
        if (result.message === 'no pending deploys') {
          pushToast('No pending deploys', 'neutral');
        } else if (result.failed.length === 0) {
          pushToast(`Approved ${result.approved.length} deploys`, 'success');
        } else {
          const reasons = result.failed.map(f => f.reason).join(', ');
          pushToast(
            `Approved ${result.approved.length}, failed ${result.failed.length} (${reasons})`,
            'warn',
          );
        }
        return result.approved;
      } catch (e) {
        pushToast(`Approve all failed: ${e instanceof api.ApiError ? e.body : 'Unknown error'}`, 'error');
        return [];
      } finally {
        refreshDeploys();
      }
    }
  }, [pushToast, refreshDeploys]);

  // Menu toggle
  const handleMenuToggle = useCallback(() => {
    if (isMobile) setMobileMenuOpen(o => !o);
    else setShowSidebar(s => !s);
  }, [isMobile]);

  // Leaving the phone width closes the drawer for good, so it does not
  // reopen when the window narrows again.
  if (!isMobile && mobileMenuOpen) setMobileMenuOpen(false);

  const nodesConnected = nodes.filter(n => n.connected === 1).length;
  const sidebarVisible = !isMobile && showSidebar;
  const menuLabel = isMobile ? 'Open menu' : showSidebar ? 'Hide sidebar' : 'Show sidebar';
  const countWhenLoaded = (load: LoadState, count: number) => (load.status === 'ready' ? count : null);
  const navCounts = {
    pendingCount: countWhenLoaded(pendingLoad, pending.length),
    nodesConnected: countWhenLoaded(nodesLoad, nodesConnected),
    historyCount: countWhenLoaded(historyLoad, history.length),
  };

  return (
    <div className="app">
      <Topbar
        wsStatus={wsStatus}
        theme={theme}
        onThemeChange={setTheme}
        menuLabel={menuLabel}
        onMenuToggle={handleMenuToggle}
      />

      <div className={sidebarVisible ? 'shell has-side' : 'shell'}>
        {sidebarVisible && <Sidebar page={page} setPage={setPage} {...navCounts} />}
        <main className="content scroll">
          <div className="content-inner">
            {page === 'pending' && (
              <PendingView
                deploys={pending}
                load={pendingLoad}
                onRetry={retryDeploys}
                onApprove={handleApprove}
                onReject={handleReject}
                onApproveAll={handleApproveAll}
              />
            )}
            {page === 'nodes' && (
              <NodesView
                nodes={nodes}
                load={nodesLoad}
                onRetry={retryNodes}
                onServiceCommand={handleServiceCommand}
                lookupInFlight={lookupByService}
              />
            )}
            {page === 'history' && (
              <HistoryView
                deploys={history}
                load={historyLoad}
                onRetry={retryDeploys}
                includeSuperseded={includeSupersededHistory}
                onToggleSuperseded={setIncludeSupersededHistory}
              />
            )}
          </div>
        </main>
      </div>

      <MobileNav page={page} setPage={setPage} pendingCount={navCounts.pendingCount ?? 0} />
      <Toaster toasts={toasts} onDismiss={dismissToast} />
      <Drawer
        open={isMobile && mobileMenuOpen}
        onClose={() => setMobileMenuOpen(false)}
        page={page}
        setPage={setPage}
        {...navCounts}
      />
    </div>
  );
}

export default App;
