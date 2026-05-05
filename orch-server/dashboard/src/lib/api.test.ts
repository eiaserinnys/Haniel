import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  fetchPending,
  fetchNodes,
  fetchHistory,
  approveDeploy,
  rejectDeploy,
  approveAll,
  ApiError,
} from './api';

// Tests assume no auth token (localStorage.clear() in beforeEach), so the
// request() wrapper does not add an Authorization header — assertions can
// match the fetch call exactly without needing to account for Bearer.
describe('api client', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('fetchPending calls /api/orch/pending', async () => {
    const mockResponse = { deploys: [] };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockResponse), { status: 200 }),
    );
    const result = await fetchPending();
    expect(result).toEqual(mockResponse);
    expect(fetch).toHaveBeenCalledWith('/api/orch/pending', { headers: {} });
  });

  it('fetchNodes calls /api/orch/nodes', async () => {
    const mockResponse = { nodes: [] };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockResponse), { status: 200 }),
    );
    const result = await fetchNodes();
    expect(result).toEqual(mockResponse);
    expect(fetch).toHaveBeenCalledWith('/api/orch/nodes', { headers: {} });
  });

  it('fetchHistory calls /api/orch/history', async () => {
    const mockResponse = { deploys: [] };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockResponse), { status: 200 }),
    );
    const result = await fetchHistory();
    expect(result).toEqual(mockResponse);
    expect(fetch).toHaveBeenCalledWith('/api/orch/history', { headers: {} });
  });

  it('approveDeploy POSTs to /api/orch/approve with deploy_id in body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'deploying' }), { status: 200 }),
    );
    await approveDeploy('test-id');
    expect(fetch).toHaveBeenCalledWith('/api/orch/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ deploy_id: 'test-id' }),
    });
  });

  it('rejectDeploy POSTs to /api/orch/reject with deploy_id and reason', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'rejected' }), { status: 200 }),
    );
    await rejectDeploy('test-id', 'bad deploy');
    expect(fetch).toHaveBeenCalledWith('/api/orch/reject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ deploy_id: 'test-id', reason: 'bad deploy' }),
    });
  });

  it('approveAll POSTs to /api/orch/approve-all', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ approved: ['a', 'b'], failed: [] }), { status: 200 }),
    );
    const result = await approveAll();
    expect(result.approved).toEqual(['a', 'b']);
    expect(fetch).toHaveBeenCalledWith('/api/orch/approve-all', {
      method: 'POST',
      headers: {},
    });
  });

  it('throws ApiError on non-ok response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Not found', { status: 404 }),
    );
    await expect(fetchPending()).rejects.toThrow(ApiError);
    try {
      await fetchPending();
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).status).toBe(404);
    }
  });

  it('passes deploy_id verbatim in body (no URL encoding needed)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'deploying' }), { status: 200 }),
    );
    await approveDeploy('node:repo/branch');
    expect(fetch).toHaveBeenCalledWith('/api/orch/approve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ deploy_id: 'node:repo/branch' }),
    });
  });
});
