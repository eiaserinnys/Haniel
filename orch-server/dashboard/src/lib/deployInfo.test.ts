import { describe, expect, it } from 'vitest';
import {
  approveAllNotices,
  deployingLabel,
  describeApiError,
  failureOutcome,
  failureReason,
  pollText,
} from './deployInfo';
import type { Deploy } from '@/types';

describe('describeApiError', () => {
  const err = (status: number, body: string) => Object.assign(new Error(`API ${status}: ${body}`), { status, body });

  it('takes the error value out of a JSON body', () => {
    expect(describeApiError(err(503, '{"error": "node not connected"}'))).toBe('node not connected');
  });

  it('shows the two self-update 409 sentences as they are', () => {
    const deploying = 'self-update haniel:abc is deploying on node n1; approve other deployments after it finishes';
    expect(describeApiError(err(409, JSON.stringify({ error: deploying, blocking_self_update: { status: 'deploying' } })))).toBe(deploying);
    const pending = 'self-update haniel:abc must be approved or postponed before other deployments on node n1';
    expect(describeApiError(err(409, JSON.stringify({ error: pending })))).toBe(pending);
  });

  it('shows the status and a short piece of text when the body is not JSON', () => {
    expect(describeApiError(err(502, 'Bad Gateway'))).toBe('502 Bad Gateway');
    expect(describeApiError(err(500, 'x'.repeat(400)))).toBe(`500 ${'x'.repeat(117)}...`);
    expect(describeApiError(err(500, '{"detail": "no error key"}'))).toBe('500 {"detail": "no error key"}');
  });

  it('falls back to the message of a plain error', () => {
    expect(describeApiError(new Error('network down'))).toBe('network down');
    expect(describeApiError('weird')).toBe('Unknown error');
  });
});

describe('failure text', () => {
  it('prefers the node error, then the reason code, then says nothing was recorded', () => {
    const base = { kind: 'orchestrator_attempt', attempt_id: 'a', at: null };
    expect(failureReason({ ...base, reason: 'deploy_result_failed', error: 'line one\nline two' })).toBe('line one line two');
    expect(failureReason({ ...base, reason: 'out_of_band_failure', error: null })).toBe('out of band failure');
    expect(failureReason(null)).toBe('no details recorded');
  });
});

describe('failureOutcome', () => {
  const failed = (extra: Partial<Deploy>): Deploy => ({
    deploy_id: 'h', node_id: 'n', repo: 'r', branch: 'main', status: 'failed', commits: [], affected_services: [],
    diff_stat: null, detected_at: '', approved_by: null, reject_reason: null, error: null, duration_ms: null,
    created_at: '', updated_at: '', ...extra,
  });

  it('writes the stage phrase and appends the rollback only when the hub says one ran', () => {
    const phrases: Record<string, string> = {
      preparing: 'preparing', build: 'building', preflight: 'checking', backing_up: 'backing up',
      migrating: 'migrating', starting: 'starting services', verifying: 'verifying',
    };
    for (const [stage, phrase] of Object.entries(phrases)) {
      expect(failureOutcome(failed({ failed_stage: stage }))).toBe(`Failed while ${phrase}`);
      expect(failureOutcome(failed({ failed_stage: stage, recovery_attempted: false }))).toBe(`Failed while ${phrase}`);
      expect(failureOutcome(failed({ failed_stage: stage, recovery_attempted: true }))).toBe(`Failed while ${phrase} · rollback ran`);
    }
  });

  it('falls back to a short sentence for the reason when no stage is known', () => {
    const lineFor = (terminal_reason: string) => failureOutcome(failed({ failed_stage: null, terminal_reason }));
    expect(lineFor('deploy_result_failed')).toBe('Deploy failed');
    expect(lineFor('node_restarted')).toBe('Node restarted mid-deploy');
    expect(lineFor('execution_unknown')).toBe('Node lost the attempt');
    expect(lineFor('approval_send_failed')).toBe('Approval not delivered');
    expect(lineFor('permit_not_acknowledged')).toBe('No response from node');
    expect(lineFor('queued_node_unreachable')).toBe('No response from node');
    expect(lineFor('ack_timeout')).toBe('No response from node');
    expect(lineFor('attempt_timeout')).toBe('No response from node');
    expect(lineFor('out_of_band_failure')).toBe('Node-run deploy failed');
  });

  it('says nothing for other reasons, old rows, and rows that did not fail', () => {
    expect(failureOutcome(failed({ failed_stage: null, terminal_reason: 'something_new' }))).toBeNull();
    expect(failureOutcome(failed({}))).toBeNull();
    expect(failureOutcome(failed({ failed_stage: 'starting', status: 'success' }))).toBeNull();
  });

  it('never guesses a rollback from the hub-side columns or the error text', () => {
    const row = failed({ terminal_reason: 'deploy_result_failed', error: 'Rolled back to abc' }) as Deploy & Record<string, unknown>;
    row.terminal_kind = 'deploy_result_failed';
    row.terminal_stage = 'starting';
    expect(failureOutcome(row)).toBe('Deploy failed');
  });
});

describe('stage and polling labels', () => {
  it('shows Deploying alone until a stage is known', () => {
    expect(deployingLabel(null)).toBe('Deploying');
    expect(deployingLabel('build')).toBe('Deploying · Building');
    expect(deployingLabel('recovering')).toBe('Deploying · Rolling back');
    expect(deployingLabel('some_new_stage')).toBe('Deploying · some new stage');
  });

  it('writes the polling interval', () => {
    expect(pollText(60)).toBe('60s');
    expect(pollText(300)).toBe('5m');
  });
});

describe('approveAllNotices', () => {
  it('adds the skipped line when requests need a look', () => {
    expect(approveAllNotices({
      approved: ['a'], failed: [], needs_inspection: [{ deploy_id: 'x', reason: 'r' }, { deploy_id: 'y', reason: 'r' }],
    })).toEqual([
      { text: 'Approved 1 deploys', kind: 'success' },
      { text: '2 skipped: last deploy failed. Retry them one by one.', kind: 'warn' },
    ]);
  });

  it('does not claim an approval when everything was skipped', () => {
    expect(approveAllNotices({ approved: [], failed: [], needs_inspection: [{ deploy_id: 'x', reason: 'r' }] }))
      .toEqual([{ text: '1 skipped: last deploy failed. Retry them one by one.', kind: 'warn' }]);
  });

  it('keeps the earlier messages', () => {
    expect(approveAllNotices({ approved: [], failed: [], message: 'no pending deploys' }))
      .toEqual([{ text: 'No pending deploys', kind: 'neutral' }]);
    expect(approveAllNotices({ approved: ['a'], failed: [{ deploy_id: 'b', reason: 'node not connected' }] }))
      .toEqual([{ text: 'Approved 1, failed 1 (node not connected)', kind: 'warn' }]);
  });
});
