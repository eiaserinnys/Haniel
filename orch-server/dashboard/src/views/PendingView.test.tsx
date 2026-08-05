import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
    is_self_update: false,
    ...overrides,
  };
}

describe('PendingView', () => {
  it('prioritizes self-updates and blocks only regular deploys on the same node', async () => {
    const onApproveAll = vi.fn().mockResolvedValue([]);
    render(
      <PendingView
        deploys={[
          deploy({ deploy_id: 'self', repo: 'haniel', node_id: 'node-1', is_self_update: true }),
          deploy({ deploy_id: 'blocked', repo: 'soulstream', node_id: 'node-1' }),
          deploy({ deploy_id: 'other', repo: 'website', node_id: 'node-2' }),
        ]}
        onApprove={vi.fn().mockResolvedValue(false)}
        onReject={vi.fn()}
        onApproveAll={onApproveAll}
      />,
    );

    const selfHeading = screen.getByRole('heading', { name: 'Self-updates' });
    const repoHeading = screen.getByRole('heading', { name: 'Repository updates' });
    expect(selfHeading.compareDocumentPosition(repoHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    const selfCard = screen.getByText('haniel').closest('.pending-card') as HTMLElement;
    const blockedCard = screen.getByText('soulstream').closest('.pending-card') as HTMLElement;
    const otherCard = screen.getByText('website').closest('.pending-card') as HTMLElement;

    expect(within(selfCard).getByRole('button', { name: 'Postpone' })).toBeEnabled();
    expect(within(blockedCard).getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(within(blockedCard).getByRole('checkbox')).toBeDisabled();
    expect(within(blockedCard).getByText('Self-update required first')).toBeInTheDocument();
    expect(within(otherCard).getByRole('button', { name: 'Approve' })).toBeEnabled();

    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByRole('button', { name: 'Approve 2 selected' }));
    await waitFor(() => expect(onApproveAll).toHaveBeenCalledWith(['self', 'other']));
  });

  it('labels the self-update rejection flow as postponement', () => {
    render(
      <PendingView
        deploys={[deploy({ deploy_id: 'self', repo: 'haniel', is_self_update: true })]}
        onApprove={vi.fn().mockResolvedValue(false)}
        onReject={vi.fn()}
        onApproveAll={vi.fn().mockResolvedValue([])}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Postpone' }));

    expect(screen.getByRole('heading', { name: 'Postpone self-update' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Postpone self-update' })).toBeDisabled();
  });

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
        onApprove={vi.fn().mockResolvedValue(false)}
        onReject={vi.fn()}
        onApproveAll={vi.fn().mockResolvedValue([])}
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
        onApprove={vi.fn().mockResolvedValue(false)}
        onReject={vi.fn()}
        onApproveAll={onApproveAll}
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
      onApprove: vi.fn().mockResolvedValue(false),
      onReject: vi.fn(),
      onApproveAll,
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

  it('keeps failure details out of the Pending screen', () => {
    render(
      <PendingView
        deploys={[]}
        onApprove={vi.fn().mockResolvedValue(false)}
        onReject={vi.fn()}
        onApproveAll={vi.fn().mockResolvedValue([])}
      />,
    );
    expect(screen.queryByText('Last deploy failed')).not.toBeInTheDocument();
    expect(screen.queryByText('post-pull failed')).not.toBeInTheDocument();
    expect(screen.getByText('All clear')).toBeInTheDocument();
  });

  it('clears a selected card after its single approve request is accepted', async () => {
    const onApprove = vi.fn().mockResolvedValue(true);
    render(
      <PendingView
        deploys={[deploy({ deploy_id: 'single' })]}
        onApprove={onApprove}
        onReject={vi.fn()}
        onApproveAll={vi.fn().mockResolvedValue([])}
      />,
    );
    fireEvent.click(screen.getByText('Select all'));

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(screen.queryByRole('button', { name: /selected/ })).not.toBeInTheDocument());
    expect(onApprove).toHaveBeenCalledWith('single');
  });

  it('keeps a selected card when its single approve request is rejected', async () => {
    const onApprove = vi.fn().mockResolvedValue(false);
    render(
      <PendingView
        deploys={[deploy({ deploy_id: 'single' })]}
        onApprove={onApprove}
        onReject={vi.fn()}
        onApproveAll={vi.fn().mockResolvedValue([])}
      />,
    );
    fireEvent.click(screen.getByText('Select all'));

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(onApprove).toHaveBeenCalledWith('single'));
    expect(screen.getByRole('button', { name: /Approve 1 selected/ })).toBeInTheDocument();
  });
});
