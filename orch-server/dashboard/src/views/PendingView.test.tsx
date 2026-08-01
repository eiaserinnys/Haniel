import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PendingView } from './PendingView';
import type { Deploy } from '@/types';

function deploy(overrides: Partial<Deploy> = {}): Deploy {
  return {
    deploy_id: 'node-1:repo:main:abc123',
    node_id: 'node-1',
    repo: 'repo',
    branch: 'main',
    status: 'pending',
    commits: ['abc123 first commit'],
    affected_services: [],
    diff_stat: null,
    detected_at: new Date().toISOString(),
    approved_by: null,
    reject_reason: null,
    error: null,
    duration_ms: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('PendingView', () => {
  it('preserves line breaks for git diff stat in expanded deploy cards', () => {
    render(
      <PendingView
        deploys={[
          deploy({
            diff_stat: [
              ' src/App.tsx       | 12 ++++++------',
              ' src/index.css     |  4 ++--',
              ' 2 files changed, 8 insertions(+), 8 deletions(-)',
            ].join('\n'),
          }),
        ]}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onApproveAll={vi.fn().mockResolvedValue([])}
        latestFailure={null}
      />,
    );

    fireEvent.click(screen.getByLabelText('Expand'));

    const diffStat = screen.getByLabelText('Git changes');
    expect(diffStat.textContent).toBe(
      [
        ' src/App.tsx       | 12 ++++++------',
        ' src/index.css     |  4 ++--',
        ' 2 files changed, 8 insertions(+), 8 deletions(-)',
      ].join('\n'),
    );
    expect(screen.getByText('2 files changed, 8 insertions(+), 8 deletions(-)')).toBeInTheDocument();
  });

  it('clears accepted selections and keeps HTTP-rejected selections only', async () => {
    const onApproveAll = vi.fn().mockResolvedValue(['accepted']);
    render(
      <PendingView
        deploys={[
          deploy({ deploy_id: 'accepted', repo: 'accepted' }),
          deploy({ deploy_id: 'rejected', repo: 'rejected' }),
        ]}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onApproveAll={onApproveAll}
        latestFailure={null}
      />,
    );

    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByRole('button', { name: /Approve 2 selected/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: /Approve 1 selected/ })).toBeInTheDocument());
    expect((screen.getAllByRole('checkbox')[1] as HTMLInputElement).checked).toBe(false);
    expect((screen.getAllByRole('checkbox')[2] as HTMLInputElement).checked).toBe(true);
  });

  it('prunes stale selections and does not reselect a reopened deterministic id', async () => {
    const onApproveAll = vi.fn().mockResolvedValue(['same-id']);
    const props = {
      onApprove: vi.fn(),
      onReject: vi.fn(),
      onApproveAll,
      latestFailure: null,
    };
    const { rerender } = render(
      <PendingView
        deploys={[deploy({ deploy_id: 'same-id', created_at: '2026-08-01T00:00:00Z' })]}
        {...props}
      />,
    );
    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByRole('button', { name: /Approve 1 selected/ }));
    await waitFor(() => expect(screen.queryByRole('button', { name: /selected/ })).not.toBeInTheDocument());

    rerender(<PendingView deploys={[]} {...props} />);
    rerender(
      <PendingView
        deploys={[deploy({ deploy_id: 'same-id', created_at: '2026-08-01T00:00:00Z' })]}
        {...props}
      />,
    );
    expect((screen.getAllByRole('checkbox')[1] as HTMLInputElement).checked).toBe(false);
  });

  it('shows latest failure beside All clear', () => {
    render(
      <PendingView
        deploys={[]}
        latestFailure={deploy({ status: 'failed', error: 'post-pull failed' })}
        onApprove={vi.fn()}
        onReject={vi.fn()}
        onApproveAll={vi.fn().mockResolvedValue([])}
      />,
    );
    expect(screen.getByText('Last deploy failed')).toBeInTheDocument();
    expect(screen.getByText('post-pull failed')).toBeInTheDocument();
    expect(screen.getByText('All clear')).toBeInTheDocument();
  });
});
