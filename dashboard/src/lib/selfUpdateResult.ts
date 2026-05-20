import type { SelfUpdateResult } from './types'

const HANDLED_SELF_UPDATE_RESULT_KEY = 'haniel-handled-self-update-result'

export function selfUpdateResultKey(result: SelfUpdateResult): string {
  return `${result.started_at}|${result.finished_at}|${result.ok}`
}

export function isHandledSelfUpdateResult(result: SelfUpdateResult): boolean {
  if (typeof sessionStorage === 'undefined') return false
  return sessionStorage.getItem(HANDLED_SELF_UPDATE_RESULT_KEY) === selfUpdateResultKey(result)
}

export function markHandledSelfUpdateResult(result: SelfUpdateResult): void {
  if (typeof sessionStorage === 'undefined') return
  sessionStorage.setItem(HANDLED_SELF_UPDATE_RESULT_KEY, selfUpdateResultKey(result))
}
