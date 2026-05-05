import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fetchPending, fetchNodes, fetchHistory, approveDeploy, rejectDeploy, approveAll, ApiError } from './api';

describe('api client', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('fetchPending calls correct endpoint', async () => {
    const mockResponse = { deploys: [] };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockResponse), { status: 200 })
    );
    const result = await fetchPending();
    expect(result).toEqual(mockResponse);
    expect(fetch).toHaveBeenCalledWith('/api/deploys/pending', undefined);
  });

  it('fetchNodes calls correct endpoint', async () => {
    const mockResponse = { nodes: [] };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockResponse), { status: 200 })
    );
    const result = await fetchNodes();
    expect(result).toEqual(mockResponse);
    expect(fetch).toHaveBeenCalledWith('/api/nodes', undefined);
  });

  it('fetchHistory calls correct endpoint', async () => {
    const mockResponse = { deploys: [] };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockResponse), { status: 200 })
    );
    const result = await fetchHistory();
    expect(result).toEqual(mockResponse);
  });

  it('approveDeploy sends POST', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'approved' }), { status: 200 })
    );
    await approveDeploy('test-id');
    expect(fetch).toHaveBeenCalledWith('/api/deploys/test-id/approve', { method: 'POST' });
  });

  it('rejectDeploy sends POST with reason', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'rejected' }), { status: 200 })
    );
    await rejectDeploy('test-id', 'bad deploy');
    expect(fetch).toHaveBeenCalledWith('/api/deploys/test-id/reject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'bad deploy' }),
    });
  });

  it('approveAll sends POST', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ approved: ['a', 'b'] }), { status: 200 })
    );
    const result = await approveAll();
    expect(result.approved).toEqual(['a', 'b']);
  });

  it('throws ApiError on non-ok response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Not found', { status: 404 })
    );
    await expect(fetchPending()).rejects.toThrow(ApiError);
    try {
      await fetchPending();
    } catch (e) {
      expect(e).toBeInstanceOf(ApiError);
      expect((e as ApiError).status).toBe(404);
    }
  });

  it('encodes deploy id in URL', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'approved' }), { status: 200 })
    );
    await approveDeploy('node:repo/branch');
    expect(fetch).toHaveBeenCalledWith('/api/deploys/node%3Arepo%2Fbranch/approve', { method: 'POST' });
  });
});
