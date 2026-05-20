import { beforeEach, describe, expect, it } from 'vitest'
import type { SelfUpdateResult } from './types'
import {
  isHandledSelfUpdateResult,
  markHandledSelfUpdateResult,
  selfUpdateResultKey,
} from './selfUpdateResult'

const result: SelfUpdateResult = {
  version: 1,
  started_at: '2026-05-20T01:00:00.000Z',
  finished_at: '2026-05-20T01:00:10.000Z',
  ok: true,
  steps: [],
  error: null,
}

describe('self-update result handling', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('builds a stable key from the completed update identity', () => {
    expect(selfUpdateResultKey(result)).toBe(
      '2026-05-20T01:00:00.000Z|2026-05-20T01:00:10.000Z|true',
    )
  })

  it('marks a result as handled for the current dashboard session', () => {
    expect(isHandledSelfUpdateResult(result)).toBe(false)

    markHandledSelfUpdateResult(result)

    expect(isHandledSelfUpdateResult(result)).toBe(true)
  })

  it('does not treat a different result as already handled', () => {
    markHandledSelfUpdateResult(result)

    expect(
      isHandledSelfUpdateResult({
        ...result,
        finished_at: '2026-05-20T01:00:11.000Z',
      }),
    ).toBe(false)
  })
})
