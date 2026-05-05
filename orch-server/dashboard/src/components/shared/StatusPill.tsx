import type { DeployStatus } from '@/types';

interface StatusToken {
  fg: string;
  bg: string;
  label: string;
}

const STATUS_TOKENS: Record<string, StatusToken> = {
  pending:   { fg: 'var(--amber)', bg: 'color-mix(in oklab, var(--amber) 14%, transparent)', label: 'Pending' },
  approved:  { fg: 'var(--blue)',  bg: 'color-mix(in oklab, var(--blue) 14%, transparent)',  label: 'Approved' },
  deploying: { fg: 'var(--amber)', bg: 'color-mix(in oklab, var(--amber) 14%, transparent)', label: 'Deploying' },
  success:   { fg: 'var(--green)', bg: 'color-mix(in oklab, var(--green) 14%, transparent)', label: 'Success' },
  failed:    { fg: 'var(--red)',   bg: 'color-mix(in oklab, var(--red) 14%, transparent)',   label: 'Failed' },
  rejected:  { fg: 'var(--muted)', bg: 'color-mix(in oklab, var(--muted) 18%, transparent)', label: 'Rejected' },
  running:   { fg: 'var(--green)', bg: 'color-mix(in oklab, var(--green) 14%, transparent)', label: 'running' },
  disabled:  { fg: 'var(--muted)', bg: 'color-mix(in oklab, var(--muted) 14%, transparent)', label: 'disabled' },
};

interface StatusPillProps {
  status: DeployStatus | string;
  dot?: boolean;
  size?: 'sm' | 'md';
}

export function StatusPill({ status, dot = true, size = 'md' }: StatusPillProps) {
  const tok = STATUS_TOKENS[status] || { fg: 'var(--muted)', bg: 'rgba(255,255,255,.06)', label: status };
  const small = size === 'sm';

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full font-medium"
      style={{
        color: tok.fg,
        background: tok.bg,
        fontSize: small ? 10.5 : 11.5,
        padding: small ? '2px 6px' : '2.5px 8px',
      }}
    >
      {dot && (
        <span
          className="inline-block w-1.5 h-1.5 rounded-full"
          style={{ background: tok.fg }}
        />
      )}
      {tok.label}
    </span>
  );
}

export { STATUS_TOKENS };
