type Tone = 'ok' | 'busy' | 'wait' | 'off' | 'fail';

const STATES: Record<string, [Tone, string]> = {
  pending: ['wait', 'Pending'],
  deploying: ['busy', 'Deploying'],
  success: ['ok', 'Success'],
  failed: ['fail', 'Failed'],
  rejected: ['off', 'Rejected'],
  running: ['ok', 'Running'],
  ready: ['ok', 'Ready'],
  starting: ['busy', 'Starting'],
  stopping: ['busy', 'Stopping'],
  stopped: ['off', 'Stopped'],
  crashed: ['fail', 'Crashed'],
  circuit_open: ['fail', 'Circuit open'],
  disabled: ['off', 'Disabled'],
  connected: ['ok', 'Connected'],
  disconnected: ['fail', 'Disconnected'],
  reconnecting: ['busy', 'Reconnecting…'],
  offline: ['off', 'Offline'],
  error: ['fail', 'Error'],
  manual: ['wait', 'Manual approval'],
  restarting: ['busy', 'Restarting (self-update)'],
};

interface StatusProps {
  state: string;
  // Replaces the default label, e.g. "Deploying · Building".
  label?: string;
}

export function Status({ state, label: custom }: StatusProps) {
  const [tone, label] = STATES[state] ?? ['off', state];
  return <span className={`st st-${tone}`}>{custom ?? label}</span>;
}
