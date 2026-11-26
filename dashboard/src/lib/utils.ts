import clsx, { type ClassValue } from 'clsx'

export const cx = (...inputs: ClassValue[]) => clsx(inputs)

export function formatUptime(seconds: number | null | undefined): string {
  if (seconds == null || seconds < 0) return '—'
  if (seconds < 60) return `${Math.floor(seconds)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`
}

export function formatClock(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('en-GB')
}

/** `git log --oneline` line -> hash and message. */
export function parseCommit(line: string): [string, string] {
  const space = line.indexOf(' ')
  return space < 0 ? [line, ''] : [line.slice(0, space), line.slice(space + 1)]
}

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}
