import { fireEvent, render, screen } from '@testing-library/react';
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
        onApproveAll={vi.fn()}
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
});
