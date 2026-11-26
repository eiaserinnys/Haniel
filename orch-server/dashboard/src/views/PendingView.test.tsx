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

// "Approve N selected" asks first; the dialog's Approve sends the batch.
function confirmBatch() {
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' }));
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

    const selfCard = screen.getByText('haniel').closest('.row') as HTMLElement;
    const blockedCard = screen.getByText('soulstream').closest('.row') as HTMLElement;
    const otherCard = screen.getByText('website').closest('.row') as HTMLElement;

    expect(within(selfCard).getByRole('button', { name: 'Postpone' })).toBeEnabled();
    expect(within(blockedCard).getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(within(blockedCard).getByRole('checkbox')).toBeDisabled();
    expect(within(blockedCard).getByText('Waits for self-update')).toBeInTheDocument();
    expect(within(otherCard).getByRole('button', { name: 'Approve' })).toBeEnabled();

    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByRole('button', { name: 'Approve 2 selected' }));
    confirmBatch();
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

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'Postpone self-update' })).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Postpone' })).toBeDisabled();
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

    fireEvent.click(screen.getByRole('button', { name: /repo/, expanded: false }));

    const diffStat = screen.getByLabelText('Git changes');
    expect(diffStat.textContent).toBe(
      [
        ' src/App.tsx       | 12 ++++++------',
        ' src/index.css     |  4 ++--',
        ' 2 files changed, 8 insertions(+), 8 deletions(-)',
      ].join('\n'),
    );
    expect(screen.getByText('+8 −8')).toBeInTheDocument();
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
    confirmBatch();
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
    confirmBatch();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve selected' })).toBeDisabled());

    rerender(<PendingView deploys={[]} {...props} />);
    rerender(
      <PendingView
        deploys={[deploy({ deploy_id: 'same-id', created_at: '2026-08-01T00:00:00Z' })]}
        {...props}
      />,
    );
    expect((screen.getAllByRole('checkbox')[1] as HTMLInputElement).checked).toBe(false);
  });

  it('says why nothing is waiting and points to History', () => {
    render(
      <PendingView
        deploys={[]}
        onApprove={vi.fn().mockResolvedValue(false)}
        onReject={vi.fn()}
        onApproveAll={vi.fn().mockResolvedValue([])}
      />,
    );
    expect(screen.getByText(
      'Nothing waiting. Auto-applied repositories deploy without stopping here — see History.',
    )).toBeInTheDocument();
    expect(screen.queryByText('All clear')).not.toBeInTheDocument();
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

    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve selected' })).toBeDisabled());
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

  const failure = {
    kind: 'orchestrator_attempt',
    attempt_id: 'a1',
    reason: 'deploy_result_failed',
    error: 'verification_failed: soulstream did not become ready within 60s',
    at: '2026-11-26T03:12:08Z',
  };
  const baseProps = () => ({
    onApprove: vi.fn().mockResolvedValue(false),
    onReject: vi.fn(),
    onApproveAll: vi.fn().mockResolvedValue([]),
  });

  it('marks a pending row whose last deploy failed and renames Approve to Retry', () => {
    render(
      <PendingView
        deploys={[
          deploy({ deploy_id: 'f', repo: 'failed-repo', last_failure: failure }),
          deploy({ deploy_id: 'l', repo: 'locked-repo', inspection_required: true }),
          deploy({ deploy_id: 'p', repo: 'plain-repo' }),
        ]}
        {...baseProps()}
      />,
    );
    const failedRow = screen.getByText('failed-repo').closest('.row') as HTMLElement;
    const lockedRow = screen.getByText('locked-repo').closest('.row') as HTMLElement;
    const plainRow = screen.getByText('plain-repo').closest('.row') as HTMLElement;

    expect(within(failedRow).getByText('Failed')).toBeInTheDocument();
    expect(within(failedRow).getByText(`Last deploy failed: ${failure.error}`)).toBeInTheDocument();
    expect(within(failedRow).getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(within(failedRow).queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(within(lockedRow).getByText('Last deploy failed: no details recorded')).toBeInTheDocument();
    expect(within(lockedRow).getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(within(plainRow).getByRole('button', { name: 'Approve' })).toBeInTheDocument();
    expect(within(plainRow).queryByText(/Last deploy failed/)).not.toBeInTheDocument();
  });

  it('puts the whole failure in the opened row', () => {
    render(<PendingView deploys={[deploy({ repo: 'failed-repo', last_failure: failure })]} {...baseProps()} />);
    fireEvent.click(screen.getByRole('button', { name: /failed-repo/, expanded: false }));
    expect(screen.getByText('Last failure')).toBeInTheDocument();
    expect(document.querySelector('.err-block')?.textContent).toBe(failure.error);
  });

  it('retries a failed row on its own, with a plain single approve', async () => {
    const props = baseProps();
    render(<PendingView deploys={[deploy({ deploy_id: 'f', inspection_required: true, last_failure: failure })]} {...props} />);
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(props.onApprove).toHaveBeenCalledWith('f'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('asks before a batch approval and lists what restarts', async () => {
    const props = baseProps();
    props.onApproveAll.mockResolvedValue(['a', 'b']);
    render(
      <PendingView
        deploys={[
          deploy({ deploy_id: 'a', repo: 'alpha', node_id: 'node-a' }),
          deploy({ deploy_id: 'b', repo: 'beta', node_id: 'node-b' }),
        ]}
        {...props}
      />,
    );
    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByRole('button', { name: 'Approve 2 selected' }));

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'Approve 2 deployments?' })).toBeInTheDocument();
    expect(within(dialog).getByText('Services on those nodes restart.')).toBeInTheDocument();
    for (const text of ['alpha', 'node-a', 'beta', 'node-b']) {
      expect(within(dialog).getByText(text)).toBeInTheDocument();
    }
    expect(props.onApproveAll).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(props.onApproveAll).not.toHaveBeenCalled();
  });

  it('leaves requests that need a look out of the batch and says so', async () => {
    const props = baseProps();
    props.onApproveAll.mockResolvedValue(['ok']);
    render(
      <PendingView
        deploys={[
          deploy({ deploy_id: 'ok', repo: 'fine-repo' }),
          deploy({ deploy_id: 'look', repo: 'look-repo', inspection_required: true, last_failure: failure }),
        ]}
        {...props}
      />,
    );
    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByRole('button', { name: 'Approve 2 selected' }));

    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'Approve 1 deployment?' })).toBeInTheDocument();
    expect(within(dialog).getByText('1 needs a look first — retry them one by one')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(props.onApproveAll).toHaveBeenCalledWith(['ok']));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve selected' })).toBeDisabled());
  });

  it('cannot confirm a batch made only of requests that need a look', () => {
    render(
      <PendingView
        deploys={[deploy({ deploy_id: 'look', inspection_required: true })]}
        {...baseProps()}
      />,
    );
    fireEvent.click(screen.getByText('Select all'));
    fireEvent.click(screen.getByRole('button', { name: 'Approve 1 selected' }));
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' })).toBeDisabled();
  });

  it('shows Manual approval only when the repository has auto-apply off', () => {
    render(
      <PendingView
        deploys={[
          deploy({ deploy_id: 'm', repo: 'manual-repo', apply_policy: 'approval' }),
          deploy({ deploy_id: 'a', repo: 'auto-repo', apply_policy: 'auto' }),
          deploy({ deploy_id: 'n', repo: 'old-repo', apply_policy: null }),
        ]}
        {...baseProps()}
      />,
    );
    expect(screen.getAllByText('Manual approval')).toHaveLength(1);
    const manualRow = screen.getByText('manual-repo').closest('.row') as HTMLElement;
    expect(within(manualRow).getByText('Manual approval')).toBeInTheDocument();
    fireEvent.click(within(manualRow).getByRole('button', { name: /manual-repo/, expanded: false }));
    expect(within(manualRow).getByText(/auto-apply is off for this repository/)).toBeInTheDocument();
  });

  it('explains what approving a self-update does', () => {
    render(<PendingView deploys={[deploy({ repo: 'haniel', is_self_update: true })]} {...baseProps()} />);
    expect(screen.getByText(
      'Approving restarts Haniel on that node. Its services pause briefly (overlap and adopt services keep running). Approve the repository updates below after it finishes.',
    )).toBeInTheDocument();
  });

  it('shows how many services restart next to the commit count', () => {
    render(
      <PendingView
        deploys={[
          deploy({ deploy_id: 'r', repo: 'regular', affected_services: ['a', 'b', 'c'] }),
          deploy({ deploy_id: 's', repo: 'haniel', node_id: 'node-9', is_self_update: true, affected_services: ['haniel'] }),
        ]}
        {...baseProps()}
      />,
    );
    const regular = screen.getByText('regular').closest('.row') as HTMLElement;
    const self = screen.getByText('haniel').closest('.row') as HTMLElement;
    expect(regular.querySelector('.m-sub')?.textContent).toBe('3 services');
    expect(self.querySelector('.m-sub')?.textContent).toBe('Restarts Haniel');
  });

  it('adds the reported stage to a deploying row', () => {
    render(
      <PendingView
        deploys={[
          deploy({ deploy_id: 'd1', repo: 'staged', status: 'deploying', progress_stage: 'starting' }),
          deploy({ deploy_id: 'd2', repo: 'quiet', node_id: 'node-2', status: 'deploying', progress_stage: null }),
        ]}
        {...baseProps()}
      />,
    );
    expect(screen.getByText('Deploying · Starting services')).toBeInTheDocument();
    expect(screen.getByText('Deploying')).toBeInTheDocument();
  });

  it('tells what Postpone and Reject do', () => {
    const { rerender } = render(
      <PendingView deploys={[deploy({ repo: 'haniel', is_self_update: true })]} {...baseProps()} />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Postpone' }));
    expect(within(screen.getByRole('dialog')).getByText('Haniel asks again in 6 hours by default.')).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));

    rerender(<PendingView deploys={[deploy({ repo: 'plain' })]} {...baseProps()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }));
    expect(within(screen.getByRole('dialog')).getByText(
      "This commit won't come back. The next commit on this branch brings these changes again.",
    )).toBeInTheDocument();
  });
});
