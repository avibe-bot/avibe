/**
 * How the bootstrap page reaches the native side when it loads.
 *
 * One rejected call is not proof that the app has stopped: the page keeps its
 * starting state and tries again. It calls the app stuck only when the app has
 * not answered at all for `STUCK_AFTER_MS`, measured from the page load or the
 * last answer, so a call that never settles counts as well as one that keeps
 * failing. A successful subscription, a successful status read and any status
 * event are answers; an answer after the page said "stuck" takes that back.
 */
interface ConnectOptions {
  /** Subscribes to status events. Called until it succeeds once. */
  subscribe: () => Promise<unknown>
  /** Reads and shows the current status. Skipped once an event has arrived. */
  snapshot: () => Promise<unknown>
  /** Whether a status event has arrived since the page loaded. */
  heardFromApp: () => boolean
  /** No answer for `STUCK_AFTER_MS`: the app really is not responding. */
  onStuck: (error: unknown) => void
  /** An answer came after `onStuck`. */
  onRecovered: () => void
}

/** Long enough that a slow first start never reaches it. */
const STUCK_AFTER_MS = 60_000

const FIRST_RETRY_MS = 250
const MAX_RETRY_MS = 5_000

function retryDelay(attempt: number): number {
  return Math.min(FIRST_RETRY_MS * 2 ** Math.min(attempt, 10), MAX_RETRY_MS)
}

const schedule = (callback: () => void, ms: number): (() => void) => {
  const id = setTimeout(callback, ms)
  return () => clearTimeout(id)
}

export async function connect(options: ConnectOptions): Promise<void> {
  const sleep = (ms: number) => new Promise<void>((resolve) => schedule(resolve, ms))

  let done = false
  let stuck = false
  let lastError: unknown = new Error('no answer from the app')
  let cancelWatch = () => {}

  const finish = (): void => {
    done = true
    cancelWatch()
    if (stuck) {
      stuck = false
      options.onRecovered()
    }
  }
  const watch = (): void => {
    cancelWatch()
    cancelWatch = schedule(() => {
      if (done) {
        return
      }
      // Events are arriving even though a call has not settled: the app is
      // alive and the page is already following it.
      if (options.heardFromApp()) {
        finish()
        return
      }
      if (!stuck) {
        stuck = true
        options.onStuck(lastError)
      }
    }, STUCK_AFTER_MS)
  }
  const answered = (): void => {
    if (stuck) {
      stuck = false
      options.onRecovered()
    }
    watch()
  }

  watch()
  let subscribed = false
  for (let attempt = 0; !done; attempt += 1) {
    try {
      if (!subscribed) {
        await options.subscribe()
        subscribed = true
        answered()
      }
      if (!options.heardFromApp()) {
        await options.snapshot()
      }
      finish()
    } catch (error) {
      lastError = error
      console.error('bootstrap command failed', error)
      // Status events are already arriving, so the app is alive; a failed
      // snapshot is no longer needed.
      if (subscribed && options.heardFromApp()) {
        finish()
      } else {
        await sleep(retryDelay(attempt))
      }
    }
  }
}
