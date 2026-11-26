import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App';

// The screen only calls onApproveAll([]) for the server's approve-all route, which
// no button reaches today (Approve selected is disabled with nothing selected), so
// the wiring is tested through a stand-in for PendingView.
const apiMock = vi.hoisted(() => ({
  fetchPending: vi.fn(),
  fetchNodes: vi.fn(),
  fetchHistory: vi.fn(),
  approveDeploy: vi.fn(),
  rejectDeploy: vi.fn(),
  approveAll: vi.fn(),
  serviceCommand: vi.fn(),
  ApiError: class ApiError extends Error {},
}));
const pendingProps = vi.hoisted(() => ({ onApproveAll: undefined as undefined | ((ids: string[]) => Promise<readonly string[]>) }));

vi.mock('@/lib/api', () => apiMock);
vi.mock('@/hooks/useIsMobile', () => ({ useIsMobile: () => false }));
vi.mock('@/hooks/useWebSocket', () => ({ useWebSocket: () => ({ status: 'connected', reconnect: vi.fn() }) }));
vi.mock('@/views/PendingView', () => ({
  PendingView: (props: { onApproveAll: (ids: string[]) => Promise<readonly string[]> }) => {
    pendingProps.onApproveAll = props.onApproveAll;
    return null;
  },
}));

describe('App approve-all route', () => {
  afterEach(cleanup);

  it('tells how many requests the server skipped because their last deploy failed', async () => {
    apiMock.fetchPending.mockResolvedValue({ deploys: [] });
    apiMock.fetchNodes.mockResolvedValue({ nodes: [] });
    apiMock.fetchHistory.mockResolvedValue({ deploys: [] });
    apiMock.approveAll.mockResolvedValue({
      approved: ['a'],
      failed: [],
      needs_inspection: [{ deploy_id: 'x', reason: 'r' }, { deploy_id: 'y', reason: 'r' }],
    });
    render(<App />);
    await waitFor(() => expect(pendingProps.onApproveAll).toBeDefined());

    await act(async () => { await pendingProps.onApproveAll?.([]); });

    const toasts = Array.from(document.querySelectorAll('.toast .st')).map(el => el.textContent);
    expect(toasts).toContain('Approved 1 deploys');
    expect(toasts).toContain('2 skipped: last deploy failed. Retry them one by one.');
    expect(document.querySelector('.toast.is-fail')).toBeNull();
  });
});
