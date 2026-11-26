import { absTime, timeOfDay } from '@/lib/utils';
import type { ApproveAllResponse, Deploy, LastFailure, ServingCommit } from '@/types';

const PROGRESS_LABELS: Record<string, string> = {
  preparing: 'Preparing',
  build: 'Building',
  preflight: 'Checking',
  backing_up: 'Backing up',
  migrating: 'Migrating',
  starting: 'Starting services',
  verifying: 'Verifying',
  recovering: 'Rolling back',
};

function words(code: string): string {
  return code.replaceAll('_', ' ');
}

export function progressLabel(stage: string): string {
  return PROGRESS_LABELS[stage] ?? words(stage);
}

// "Deploying" alone until the node reports a stage.
export function deployingLabel(stage: string | null | undefined): string {
  return stage ? `Deploying · ${progressLabel(stage)}` : 'Deploying';
}

function oneLine(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

// The reason shown after "Last deploy failed:". The node's error text first,
// then the server's reason code.
export function failureReason(failure: LastFailure | null | undefined): string {
  const text = failure?.error?.trim() || (failure?.reason ? words(failure.reason) : '');
  return text ? oneLine(text) : 'no details recorded';
}

export function failureDetail(failure: LastFailure | null | undefined): string {
  return failure?.error?.trim() || (failure?.reason ? words(failure.reason) : 'No details recorded');
}

export function hasFailed(deploy: Deploy): boolean {
  return Boolean(deploy.inspection_required) || Boolean(deploy.last_failure);
}

// What the History failure row says, from the failure columns of
// GET /api/orch/history (hub PR #114 v2). failed_stage is the node stage the
// deploy stopped in; recovery_attempted says the node entered recovery, not that
// it recovered (that stays in the node's error sentence). Neither is guessed
// from terminal_kind or terminal_stage, which are hub-side values.
const FAILED_WHILE: Record<string, string> = {
  preparing: 'preparing',
  build: 'building',
  preflight: 'checking',
  backing_up: 'backing up',
  migrating: 'migrating',
  starting: 'starting services',
  verifying: 'verifying',
};

const REASON_LINES: Record<string, string> = {
  deploy_result_failed: 'Deploy failed',
  node_restarted: 'Node restarted mid-deploy',
  execution_unknown: 'Node lost the attempt',
  approval_send_failed: 'Approval not delivered',
  permit_not_acknowledged: 'No response from node',
  queued_node_unreachable: 'No response from node',
  out_of_band_failure: 'Node-run deploy failed',
};

function reasonLine(reason: string | null | undefined): string | null {
  if (!reason) return null;
  if (reason.endsWith('_timeout')) return 'No response from node';
  return REASON_LINES[reason] ?? null;
}

// "Failed while starting services · recovery attempted". null when the row has
// nothing to say, so it keeps showing only its error.
export function failureOutcome(deploy: Deploy): string | null {
  if (deploy.status !== 'failed') return null;
  const phrase = deploy.failed_stage ? FAILED_WHILE[deploy.failed_stage] : undefined;
  const head = phrase ? `Failed while ${phrase}` : reasonLine(deploy.terminal_reason);
  if (!head) return null;
  return deploy.recovery_attempted === true ? `${head} · recovery attempted` : head;
}

export function servingTime(servedAt: string): string {
  const served = new Date(servedAt);
  const today = new Date();
  return served.toDateString() === today.toDateString()
    ? timeOfDay(servedAt).slice(0, 5)
    : absTime(servedAt);
}

export function servingCommit(serving: ServingCommit): string {
  return serving.target_head.slice(0, 8);
}

export function pollText(seconds: number): string {
  return seconds < 120 ? `${seconds}s` : `${Math.round(seconds / 60)}m`;
}

export function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

// ApiError.body is the response text. The hub answers {"error": "..."}; anything
// else shows the status and a short piece of the raw text.
export function describeApiError(e: unknown): string {
  if (typeof e === 'object' && e !== null && 'status' in e && 'body' in e) {
    const { status, body } = e as { status: number; body: string };
    try {
      const parsed: unknown = JSON.parse(body);
      if (typeof parsed === 'object' && parsed !== null) {
        const message = (parsed as { error?: unknown }).error;
        if (typeof message === 'string' && message.trim()) return message.trim();
      }
    } catch {
      // not JSON — fall through to the raw text
    }
    const raw = oneLine(String(body));
    const short = raw.length > 120 ? `${raw.slice(0, 117)}...` : raw;
    return short ? `${status} ${short}` : String(status);
  }
  return e instanceof Error && e.message ? e.message : 'Unknown error';
}

export function approveAllNotices(result: ApproveAllResponse): Array<{ text: string; kind: 'success' | 'warn' | 'neutral' }> {
  const notices: Array<{ text: string; kind: 'success' | 'warn' | 'neutral' }> = [];
  const skipped = result.needs_inspection?.length ?? 0;
  if (result.message === 'no pending deploys') {
    notices.push({ text: 'No pending deploys', kind: 'neutral' });
  } else if (result.failed.length === 0) {
    if (result.approved.length > 0 || skipped === 0) {
      notices.push({ text: `Approved ${result.approved.length} deploys`, kind: 'success' });
    }
  } else {
    const reasons = result.failed.map(f => f.reason).join(', ');
    notices.push({
      text: `Approved ${result.approved.length}, failed ${result.failed.length} (${reasons})`,
      kind: 'warn',
    });
  }
  if (skipped > 0) {
    notices.push({ text: `${skipped} skipped: last deploy failed. Retry them one by one.`, kind: 'warn' });
  }
  return notices;
}
