/**
 * In-memory marker for real visible user activity. The next authenticated
 * REST request consumes the marker and carries `X-AFlow-Activity: 1`, which
 * the server uses to roll the browser session forward. Background polling
 * and SSE traffic never mark activity, and the marker is never persisted.
 */
let pendingActivity = false
let lastConsumedAt = 0

export const ACTIVITY_MIN_INTERVAL_MS = 60_000

export function markUserActivity(now = Date.now()): void {
  if (pendingActivity) return
  if (now - lastConsumedAt < ACTIVITY_MIN_INTERVAL_MS) return
  pendingActivity = true
}

/**
 * Consume the pending marker at most once per minute so a single interaction
 * burst cannot renew the session on every request.
 */
export function consumeActivityMarker(now = Date.now()): boolean {
  if (!pendingActivity) return false
  if (now - lastConsumedAt < ACTIVITY_MIN_INTERVAL_MS) return false
  pendingActivity = false
  lastConsumedAt = now
  return true
}

/** Test-only reset. */
export function resetActivityMarker(): void {
  pendingActivity = false
  lastConsumedAt = 0
}
