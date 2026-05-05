import { useState, useEffect, useCallback } from 'react';
import { Sidebar } from '@/components/layout/Sidebar';
import { Topbar } from '@/components/layout/Topbar';
import { MobileNav } from '@/components/layout/MobileNav';
import { Toaster } from '@/components/shared/Toaster';
import { PendingView } from '@/views/PendingView';
import { NodesView } from '@/views/NodesView';
import { HistoryView } from '@/views/HistoryView';
import { useIsMobile } from '@/hooks/useIsMobile';
import { useWebSocket } from '@/hooks/useWebSocket';
import * as api from '@/lib/api';
import { cn } from '@/lib/utils';
import type { Page, Deploy, OrchestratorNode, Toast, WsEvent } from '@/types';

function App() {
  const [page, setPageRaw] = useState<Page>('pending');
  const [pending, setPending] = useState<Deploy[]>([]);
  const [nodes, setNodes] = useState<OrchestratorNode[]>([]);
  const [history, setHistory] = useState<Deploy[]>([]);
  const [toasts, setToasts] = useState<Toast[]>([]);
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
        // Update pending: remove if no longer pending
        if (event.status !== 'pending') {
          setPending(ps => ps.filter(p => p.deploy_id !== event.deploy_id));
        }
        // Refetch history to get latest
        api.fetchHistory().then(r => setHistory(r.deploys)).catch(() => {});
        if (event.status === 'deploying') {
          pushToast(`Deploying ${event.deploy_id.split(':')[1] || ''}`, 'amber');
        } else if (event.status === 'success') {
          pushToast(`Deploy succeeded`, 'success');
        } else if (event.status === 'failed') {
          pushToast(`Deploy failed`, 'error');
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
        if (event.success) {
          pushToast(`${event.action} ${event.service_name}: success`, 'success');
        } else {
          pushToast(`${event.action} ${event.service_name}: ${event.error || 'failed'}`, 'error');
        }
        // Refetch nodes to update service status
        api.fetchNodes().then(r => setNodes(r.nodes)).catch(() => {});
        break;
    }
  }, [pushToast]);

  const { status: wsStatus } = useWebSocket(handleWsEvent);

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
      await api.approveDeploy(deployId);
      setPending(ps => ps.filter(p => p.deploy_id !== deployId));
      pushToast(`Approved deploy`, 'success');
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
      await api.serviceCommand(nodeId, serviceName, action);
      pushToast(`${action} ${serviceName} sent`, 'info');
    } catch (e) {
      pushToast(`${action} failed: ${e instanceof api.ApiError ? e.body : 'Unknown error'}`, 'error');
    }
  }, [pushToast]);

  const handleApproveAll = useCallback(async (ids: string[]) => {
    if (ids.length > 0) {
      // Approve selected items individually
      const results = await Promise.allSettled(ids.map(id => api.approveDeploy(id)));
      const succeeded = results.filter(r => r.status === 'fulfilled').length;
      const failed = results.length - succeeded;
      setPending(ps => ps.filter(p => !ids.includes(p.deploy_id)));
      if (failed === 0) {
        pushToast(`Approved ${succeeded} deploys`, 'success');
      } else {
        pushToast(`Approved ${succeeded}, failed ${failed}`, 'amber');
      }
    } else {
      // Approve all via server API
      try {
        const result = await api.approveAll();
        setPending([]);
        pushToast(`Approved ${result.approved.length} deploys`, 'success');
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
          {page === 'nodes' && <NodesView nodes={nodes} onServiceCommand={handleServiceCommand} />}
          {page === 'history' && <HistoryView deploys={history} />}
        </main>
      </div>

      <MobileNav page={page} setPage={setPage} pendingCount={pending.length} />
      <Toaster toasts={toasts} />
    </div>
  );
}

export default App;
