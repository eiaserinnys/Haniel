import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';

const apiMock = vi.hoisted(() => ({
  fetchPending: vi.fn(),
  fetchNodes: vi.fn(),
  fetchHistory: vi.fn(),
  approveDeploy: vi.fn(),
  rejectDeploy: vi.fn(),
  approveAll: vi.fn(),
  serviceCommand: vi.fn(),
  ApiError: class ApiError extends Error {
    status: number;
    body: string;

    constructor(status: number, body: string) {
      super(`API ${status}: ${body}`);
      this.name = 'ApiError';
      this.status = status;
      this.body = body;
    }
  },
}));

const wsMock = vi.hoisted(() => ({
  status: 'connected',
  handler: undefined as undefined | ((event: unknown) => void),
}));

vi.mock('@/lib/api', () => apiMock);

vi.mock('@/hooks/useIsMobile', () => ({
  useIsMobile: () => false,
}));

vi.mock('@/hooks/useWebSocket', () => ({
  useWebSocket: (onEvent: (event: unknown) => void) => {
    wsMock.handler = onEvent;
    return { status: wsMock.status, reconnect: vi.fn() };
  },
}));

function resetApiMocks() {
  apiMock.fetchPending.mockResolvedValue({ deploys: [] });
  apiMock.fetchNodes.mockResolvedValue({ nodes: [] });
  apiMock.fetchHistory.mockResolvedValue({ deploys: [] });
  apiMock.approveDeploy.mockResolvedValue({ deploy_id: 'd1', status: 'deploying' });
  apiMock.rejectDeploy.mockResolvedValue({ deploy_id: 'd1', status: 'rejected' });
  apiMock.approveAll.mockResolvedValue({ approved: [], failed: [] });
  apiMock.serviceCommand.mockResolvedValue({ command_id: 'c1', status: 'sent' });
}

async function waitForInitialSync() {
  await waitFor(() => expect(apiMock.fetchPending).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(apiMock.fetchNodes).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(apiMock.fetchHistory).toHaveBeenCalledTimes(1));
}

// The Toaster wraps hyphenated words in spans, so match a toast by its whole text.
function toastWithText(text: string) {
  return waitFor(() => {
    const found = Array.from(document.querySelectorAll('.toast .st')).find(el => el.textContent === text);
    expect(found).toBeTruthy();
    return found as HTMLElement;
  });
}

function pendingDeploy(id: string) {
  const now = new Date().toISOString();
  return {
    deploy_id: id, node_id: 'n1', repo: id, branch: 'main', status: 'pending',
    commits: ['abc change'], affected_services: [], diff_stat: null,
    detected_at: now, approved_by: null, reject_reason: null, error: null,
    duration_ms: null, created_at: now, updated_at: now,
  };
}

describe('App dashboard sync', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    resetApiMocks();
    wsMock.status = 'connected';
    wsMock.handler = undefined;
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('refreshes deploy lists after terminal deploy websocket events', async () => {
    render(<App />);
    await waitForInitialSync();

    apiMock.fetchPending.mockClear();
    apiMock.fetchHistory.mockClear();

    act(() => {
      wsMock.handler?.({
        type: 'status_change',
        deploy_id: 'd1',
        node_id: 'n1',
        status: 'success',
      });
    });

    await waitFor(() => expect(apiMock.fetchPending).toHaveBeenCalledTimes(1));
    expect(apiMock.fetchHistory).toHaveBeenCalledWith({ includeSuperseded: false });
  });

  it('refreshes all dashboard data after websocket reconnects', async () => {
    wsMock.status = 'reconnecting';
    const { rerender } = render(<App />);
    await waitForInitialSync();

    apiMock.fetchPending.mockClear();
    apiMock.fetchNodes.mockClear();
    apiMock.fetchHistory.mockClear();

    wsMock.status = 'connected';
    rerender(<App />);

    await waitFor(() => expect(apiMock.fetchPending).toHaveBeenCalledTimes(1));
    expect(apiMock.fetchNodes).toHaveBeenCalledTimes(1);
    expect(apiMock.fetchHistory).toHaveBeenCalledWith({ includeSuperseded: false });
  });

  it('refreshes visible tabs on focus as a repair path for missed events', async () => {
    render(<App />);
    await waitForInitialSync();

    apiMock.fetchPending.mockClear();
    apiMock.fetchNodes.mockClear();
    apiMock.fetchHistory.mockClear();

    act(() => {
      window.dispatchEvent(new Event('focus'));
    });

    await waitFor(() => expect(apiMock.fetchPending).toHaveBeenCalledTimes(1));
    expect(apiMock.fetchNodes).toHaveBeenCalledTimes(1);
    expect(apiMock.fetchHistory).toHaveBeenCalledWith({ includeSuperseded: false });
  });

  it('shows the failure of a pending row and a Retry button', async () => {
    apiMock.fetchPending.mockResolvedValue({
      deploys: [{
        ...pendingDeploy('failed-one'),
        inspection_required: true,
        last_failure: { kind: 'orchestrator_attempt', attempt_id: 'a1', reason: 'deploy_result_failed', error: 'HEAD mismatch', at: null },
      }],
    });

    render(<App />);
    await waitForInitialSync();

    expect(await screen.findByText('Last deploy failed: HEAD mismatch')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('returns only HTTP-accepted selected ids to the selection owner', async () => {
    apiMock.fetchPending.mockResolvedValue({
      deploys: [pendingDeploy('accepted'), pendingDeploy('rejected')],
    });
    apiMock.approveDeploy.mockImplementation((id: string) => (
      id === 'accepted'
        ? Promise.resolve({ deploy_id: id, status: 'deploying' })
        : Promise.reject(new Error('HTTP rejected'))
    ));

    render(<App />);
    await waitForInitialSync();
    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByRole('button', { name: /Approve 2 selected/ }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Approve 1 selected/ })).toBeInTheDocument();
    });
    expect(apiMock.approveDeploy).toHaveBeenCalledWith('accepted');
    expect(apiMock.approveDeploy).toHaveBeenCalledWith('rejected');
  });

  it('toasts a failure when a deploying request comes back as pending', async () => {
    const deploying = { ...pendingDeploy('d1'), status: 'deploying', progress_stage: 'starting' };
    apiMock.fetchPending.mockResolvedValue({ deploys: [deploying] });
    render(<App />);
    await waitForInitialSync();
    expect(await screen.findByText('Deploying · Starting services')).toBeInTheDocument();

    apiMock.fetchPending.mockResolvedValue({
      deploys: [{
        ...pendingDeploy('d1'),
        inspection_required: true,
        last_failure: { kind: 'orchestrator_attempt', attempt_id: 'a', reason: 'deploy_result_failed', error: 'service did not start', at: null },
      }],
    });
    act(() => {
      wsMock.handler?.({ type: 'status_change', deploy_id: 'd1', node_id: 'n1', status: 'pending' });
    });

    expect(await screen.findByText('Deploy failed: service did not start')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });

  it('does not toast a failure for a request that is still deploying or just waits', async () => {
    apiMock.fetchPending.mockResolvedValue({ deploys: [{ ...pendingDeploy('d1'), status: 'deploying' }, pendingDeploy('d2')] });
    render(<App />);
    await waitForInitialSync();

    act(() => {
      wsMock.handler?.({ type: 'status_change', deploy_id: 'd2', node_id: 'n1', status: 'pending' });
    });
    await waitFor(() => expect(apiMock.fetchPending).toHaveBeenCalledTimes(2));
    expect(screen.queryByText(/Deploy failed/)).not.toBeInTheDocument();
  });

  it('changes the stage of a deploying row from a deploy_progress event without fetching again', async () => {
    apiMock.fetchPending.mockResolvedValue({ deploys: [{ ...pendingDeploy('d1'), status: 'deploying', progress_stage: 'build' }] });
    render(<App />);
    await waitForInitialSync();
    expect(await screen.findByText('Deploying · Building')).toBeInTheDocument();
    apiMock.fetchPending.mockClear();

    act(() => {
      wsMock.handler?.({ type: 'deploy_progress', deploy_id: 'd1', node_id: 'n1', orchestrator_attempt_id: 'a1', stage: 'verifying' });
    });

    expect(await screen.findByText('Deploying · Verifying')).toBeInTheDocument();
    expect(apiMock.fetchPending).not.toHaveBeenCalled();
  });

  it('waits instead of warning when a node drops for its own self-update', async () => {
    render(<App />);
    await waitForInitialSync();

    act(() => {
      wsMock.handler?.({ type: 'node_disconnected', node_id: 'node-a', reason: 'closed', self_update_deploying: true });
      wsMock.handler?.({ type: 'node_disconnected', node_id: 'node-b', reason: 'closed', self_update_deploying: false });
    });

    expect(await toastWithText('node-a is restarting for a self-update')).toHaveClass('st-busy');
    expect(await toastWithText('Node disconnected: node-b')).toHaveClass('st-wait');
    expect(document.querySelector('.toasts')?.textContent).not.toContain('Node disconnected: node-a');
  });

  it('shows the error value of a failed approve as a sentence', async () => {
    apiMock.fetchPending.mockResolvedValue({ deploys: [pendingDeploy('d1')] });
    apiMock.approveDeploy.mockRejectedValue(new apiMock.ApiError(503, '{"error": "node not connected", "deploy_id": "d1"}'));
    render(<App />);
    await waitForInitialSync();

    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));

    expect(await screen.findByText('Approve failed: node not connected')).toBeInTheDocument();
  });

  it('shows the self-update 409 sentence and a short raw text for a non-JSON failure', async () => {
    apiMock.fetchPending.mockResolvedValue({ deploys: [pendingDeploy('d1')] });
    const blocked = 'self-update haniel:abc is deploying on node n1; approve other deployments after it finishes';
    apiMock.approveDeploy.mockRejectedValueOnce(new apiMock.ApiError(409, JSON.stringify({ error: blocked })));
    render(<App />);
    await waitForInitialSync();
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
    await toastWithText(`Approve failed: ${blocked}`);

    apiMock.approveDeploy.mockRejectedValueOnce(new apiMock.ApiError(502, 'Bad Gateway'));
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    expect(await screen.findByText('Approve failed: 502 Bad Gateway')).toBeInTheDocument();
  });

  it('reports a plain approve as approved', async () => {
    apiMock.fetchPending.mockResolvedValue({ deploys: [pendingDeploy('d1')] });
    render(<App />);
    await waitForInitialSync();
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }));
    expect(await screen.findByText('Approved deploy')).toBeInTheDocument();
  });
});
