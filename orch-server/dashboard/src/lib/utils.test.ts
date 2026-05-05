import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { relTime, uptimeStr, durMs, timeOfDay, dateLabel, parseCommit, cn } from './utils';

describe('relTime', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('returns "just now" for < 5s', () => {
    vi.setSystemTime(new Date('2025-01-01T12:00:05Z'));
    expect(relTime('2025-01-01T12:00:02Z')).toBe('just now');
  });

  it('returns seconds for < 60s', () => {
    vi.setSystemTime(new Date('2025-01-01T12:00:30Z'));
    expect(relTime('2025-01-01T12:00:00Z')).toBe('30s ago');
  });

  it('returns minutes for < 60m', () => {
    vi.setSystemTime(new Date('2025-01-01T12:05:00Z'));
    expect(relTime('2025-01-01T12:00:00Z')).toBe('5m ago');
  });

  it('returns hours for < 24h', () => {
    vi.setSystemTime(new Date('2025-01-01T15:30:00Z'));
    expect(relTime('2025-01-01T12:00:00Z')).toBe('3h 30m ago');
  });

  it('returns days for >= 24h', () => {
    vi.setSystemTime(new Date('2025-01-03T12:00:00Z'));
    expect(relTime('2025-01-01T12:00:00Z')).toBe('2d 0h ago');
  });

  it('accepts epoch ms', () => {
    const now = new Date('2025-01-01T12:01:00Z').getTime();
    vi.setSystemTime(now);
    expect(relTime(now - 45000)).toBe('45s ago');
  });
});

describe('uptimeStr', () => {
  it('returns — for null/undefined/0', () => {
    expect(uptimeStr(null)).toBe('—');
    expect(uptimeStr(undefined)).toBe('—');
    expect(uptimeStr(0)).toBe('—');
  });

  it('returns seconds', () => {
    expect(uptimeStr(45000)).toBe('45s');
  });

  it('returns minutes', () => {
    expect(uptimeStr(5 * 60 * 1000 + 30000)).toBe('5m 30s');
  });

  it('returns hours', () => {
    expect(uptimeStr(3 * 3600 * 1000 + 15 * 60 * 1000)).toBe('3h 15m');
  });

  it('returns days', () => {
    expect(uptimeStr(2 * 86400 * 1000 + 5 * 3600 * 1000)).toBe('2d 5h');
  });
});

describe('durMs', () => {
  it('returns — for null', () => {
    expect(durMs(null)).toBe('—');
    expect(durMs(undefined)).toBe('—');
  });

  it('returns ms for < 1000', () => {
    expect(durMs(250)).toBe('250ms');
  });

  it('returns seconds for >= 1000', () => {
    expect(durMs(3500)).toBe('3.5s');
  });
});

describe('timeOfDay', () => {
  it('formats HH:MM:SS', () => {
    const result = timeOfDay('2025-01-01T08:05:03Z');
    expect(result).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });
});

describe('dateLabel', () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it('returns Today', () => {
    vi.setSystemTime(new Date('2025-06-15T10:00:00Z'));
    expect(dateLabel('2025-06-15T05:00:00Z')).toBe('Today');
  });

  it('returns Yesterday', () => {
    vi.setSystemTime(new Date('2025-06-15T10:00:00Z'));
    expect(dateLabel('2025-06-14T05:00:00Z')).toBe('Yesterday');
  });

  it('returns month + day', () => {
    vi.setSystemTime(new Date('2025-06-15T10:00:00Z'));
    const result = dateLabel('2025-06-10T10:00:00Z');
    expect(result).toContain('Jun');
    expect(result).toContain('10');
  });
});

describe('parseCommit', () => {
  it('splits hash from message', () => {
    const result = parseCommit('abc1234 fix: some bug');
    expect(result.hash).toBe('abc1234');
    expect(result.message).toBe('fix: some bug');
  });

  it('handles short commit without space', () => {
    const result = parseCommit('abc1234');
    expect(result.hash).toBe('abc1234');
    expect(result.message).toBe('');
  });
});

describe('cn', () => {
  it('joins truthy classes', () => {
    expect(cn('a', 'b', 'd')).toBe('a b d');
  });

  it('filters nullish', () => {
    expect(cn('a', null, undefined, 'b')).toBe('a b');
  });

  it('merges tailwind classes', () => {
    expect(cn('p-4', 'p-2')).toBe('p-2');
  });
});
