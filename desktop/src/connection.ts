/**
 * How the bootstrap page reaches the native side when it loads.
 *
 * One rejected call is not proof that the app has stopped: on a cold first
 * start the window can come up while the native side is still busy, and an
 * early call can fail even though the app goes on to start normally. So the
 * page keeps its starting state and tries again, and calls the app stuck only
 * after it has had no answer at all for `stuckAfterMs`. Any status event or
 * successful answer counts as an answer, including one that arrives after the
 * page has already said the app is stuck.
 */
export interface ConnectOptions {
  /** Subscribes to status events. Called until it succeeds once. */
  subscribe: () => Promise<unknown>
  /** Reads and shows the current status. Skipped once an event has arrived. */
  snapshot: () => Promise<unknown>
  /** Whether a status event has arrived since the page loaded. */
  heardFromApp: () => boolean
  /** No answer for `stuckAfterMs`: the app really is not responding. */
  onStuck: (error: unknown) => void
  /** An answer came after `onStuck`. */
  onRecovered: () => void
  now?: () => number
  sleep?: (ms: number) => Promise<void>
  stuckAfterMs?: number
}

/** Long enough that a slow first start never reaches it. */
export const STUCK_AFTER_MS = 60_000

const FIRST_RETRY_MS = 250
const MAX_RETRY_MS = 5_000

export function retryDelay(attempt: number): number {
  return Math.min(FIRST_RETRY_MS * 2 ** Math.min(attempt, 10), MAX_RETRY_MS)
}

export async function connect(options: ConnectOptions): Promise<void> {
  const now = options.now ?? Date.now
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)))
  const stuckAfterMs = options.stuckAfterMs ?? STUCK_AFTER_MS

  let subscribed = false
  let firstFailure: number | null = null
  let stuck = false

  for (let attempt = 0; ; attempt += 1) {
    try {
      if (!subscribed) {
        await options.subscribe()
        subscribed = true
      }
      if (!options.heardFromApp()) {
        await options.snapshot()
      }
      if (stuck) {
        options.onRecovered()
      }
      return
    } catch (error) {
      console.error('bootstrap command failed', error)
      // Status events are already arriving, so the app is alive and the page
      // is following it; a failed snapshot is no longer needed.
      if (subscribed && options.heardFromApp()) {
        if (stuck) {
          options.onRecovered()
        }
        return
      }
      firstFailure ??= now()
      if (!stuck && now() - firstFailure >= stuckAfterMs) {
        stuck = true
        options.onStuck(error)
      }
      await sleep(retryDelay(attempt))
    }
  }
}
