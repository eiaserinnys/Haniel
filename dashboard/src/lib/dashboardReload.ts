export function scheduleDashboardReload(delayMs = 200): void {
  window.setTimeout(() => window.location.reload(), delayMs)
}
