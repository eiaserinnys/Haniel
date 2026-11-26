import { render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { HistoryView } from './HistoryView';
import type { Deploy } from '@/types';

function row(overrides: Partial<Deploy>): Deploy {
  const now = new Date().toISOString();
  return {
    deploy_id: 'h1', node_id: 'n1', repo: 'repo', branch: 'main', status: 'failed',
    commits: ['abc1234 change'], affected_services: [], diff_stat: null, detected_at: now,
    approved_by: 'director', reject_reason: null, error: null, duration_ms: 1000,
    created_at: now, updated_at: now, ...overrides,
  };
}

function renderRows(deploys: Deploy[]) {
  render(<HistoryView deploys={deploys} includeSuperseded={false} onToggleSuperseded={vi.fn()} />);
}

describe('HistoryView failure rows', () => {
  it('names the stage the deploy failed in and whether recovery was attempted, in one line', () => {
    renderRows([
      row({
        deploy_id: 'f1', repo: 'rolled-repo', terminal_reason: 'deploy_result_failed',
        failed_stage: 'starting', recovery_attempted: true,
        error: 'verification_failed ... availability recovered',
      }),
      row({ deploy_id: 'f2', repo: 'plain-stage-repo', failed_stage: 'verifying', recovery_attempted: false }),
      row({ deploy_id: 'f3', repo: 'silent-repo', failed_stage: 'build', recovery_attempted: null }),
      row({ deploy_id: 'f4', repo: 'timeout-repo', terminal_reason: 'ack_timeout', failed_stage: null }),
      row({ deploy_id: 'f5', repo: 'old-repo' }),
      row({ deploy_id: 'f6', repo: 'odd-repo', terminal_reason: 'something_new', failed_stage: null }),
      row({ deploy_id: 's1', repo: 'ok-repo', status: 'success', failed_stage: 'starting' }),
    ]);
    const lineOf = (repo: string) => (screen.getByText(repo).closest('.row') as HTMLElement).querySelector('.h-outcome');
    expect(lineOf('rolled-repo')?.textContent).toBe('Failed while starting services · recovery attempted');
    expect(lineOf('plain-stage-repo')?.textContent).toBe('Failed while verifying');
    expect(lineOf('silent-repo')?.textContent).toBe('Failed while building');
    expect(lineOf('timeout-repo')?.textContent).toBe('No response from node');
    expect(lineOf('old-repo')).toBeNull();
    expect(lineOf('odd-repo')).toBeNull();
    expect(lineOf('ok-repo')).toBeNull();
    expect(within(screen.getByText('old-repo').closest('.row') as HTMLElement).getByText('Failed')).toBeInTheDocument();
  });

  it('keeps the node error available as it was', () => {
    renderRows([row({ repo: 'err-repo', error: 'recovery failed', failed_stage: 'migrating', recovery_attempted: true })]);
    expect(screen.getByRole('button', { name: 'Show error' })).toBeInTheDocument();
  });
});
