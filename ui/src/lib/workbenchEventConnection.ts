export type WorkbenchEventConnectionState = 'connected' | 'reconnecting';

export const WORKBENCH_EVENT_RETRY_INITIAL_MS = 1_000;
export const WORKBENCH_EVENT_RETRY_MAX_MS = 15_000;
export const WORKBENCH_EVENT_OPEN_TIMEOUT_MS = 20_000;

/**
 * Heartbeat cadence to assume when the server has not declared a usable one.
 * Only ever sizes the window around a heartbeat that did arrive: a stream is
 * unproven until one does, so a server too old to send them never reads as
 * proven no matter what this says.
 */
export const WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS = 15_000;

// How many heartbeats a stream may miss before it stops speaking for itself.
// Lower turns one dropped frame or a long GC pause into a phantom outage, and
// the only cost of waiting is one catch-up read arriving late.
const WORKBENCH_EVENT_HEARTBEAT_MISSES = 3;
// Floor, so a fast cadence still tolerates ordinary scheduling jitter.
const WORKBENCH_EVENT_HEARTBEAT_STALE_FLOOR_MS = 45_000;
// Bounds on a cadence the server declares. A nonsense value must not be able to
// switch the staleness check off by claiming an interval no gap could exceed.
const WORKBENCH_EVENT_HEARTBEAT_MIN_MS = 1_000;
const WORKBENCH_EVENT_HEARTBEAT_MAX_MS = 120_000;

/**
 * A cadence the server actually declared, or undefined when it declared none.
 *
 * The distinction carries real weight: a declaration is what makes a deadline
 * enforceable, so a server that never made one must not be held to a default it
 * never agreed to. Substituting the fallback here would put every stream from a
 * server too old to declare a cadence on a deadline it cannot meet, and recycle
 * it every stale window forever.
 */
export function declaredWorkbenchHeartbeatInterval(raw: unknown): number | undefined {
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return undefined;
  return Math.min(
    WORKBENCH_EVENT_HEARTBEAT_MAX_MS,
    Math.max(WORKBENCH_EVENT_HEARTBEAT_MIN_MS, raw),
  );
}

/**
 * Read a server-declared cadence, falling back on anything unusable. For a
 * frame that is itself proof of life -- a heartbeat -- an unreadable cadence is
 * not worth discarding the proof over, so the fallback sizes the next window.
 */
export function parseWorkbenchHeartbeatInterval(raw: unknown): number {
  return declaredWorkbenchHeartbeatInterval(raw) ?? WORKBENCH_EVENT_HEARTBEAT_FALLBACK_MS;
}

/** How long a stream's last proof of life stays good. */
export function workbenchEventStaleAfterMs(intervalMs: number): number {
  return Math.max(
    WORKBENCH_EVENT_HEARTBEAT_STALE_FLOOR_MS,
    intervalMs * WORKBENCH_EVENT_HEARTBEAT_MISSES,
  );
}

/**
 * Whether a stream has proved itself alive recently enough to account for a gap
 * the page spent away. `readyState === OPEN` cannot: a suspended tab's socket
 * can die with the connection left in a zombie `OPEN` state and no `error`
 * event, which is precisely the case a returning page must not trust.
 */
export function isWorkbenchHeartbeatFresh(
  lastHeartbeatAt: number | null,
  intervalMs: number,
  now: number,
): boolean {
  if (lastHeartbeatAt === null) return false;
  const age = now - lastHeartbeatAt;
  // An age outside the window in either direction is unusable: the clock moved
  // backwards, so the stamp cannot be dated. Read that as unproven, which costs
  // one redundant read instead of trusting a stream that may be dead.
  return age >= 0 && age < workbenchEventStaleAfterMs(intervalMs);
}

type TimerHandle = ReturnType<typeof setTimeout>;

interface WorkbenchEventReconnectLoopOptions {
  reconnect: () => void;
  isVisible: () => boolean;
  /** Whether events are flowing end to end right now, so no gap needs closing. */
  isStreamLive: () => boolean;
}

/** Owns the retry policy; EventSource wiring stays in ApiContext. */
export class WorkbenchEventReconnectLoop {
  private readonly reconnect: () => void;
  private readonly isVisible: () => boolean;
  private readonly isStreamLive: () => boolean;
  private retryTimer: TimerHandle | null = null;
  private openTimer: TimerHandle | null = null;
  private retryAttempt = 0;
  private stopped = false;

  constructor(options: WorkbenchEventReconnectLoopOptions) {
    this.reconnect = options.reconnect;
    this.isVisible = options.isVisible;
    this.isStreamLive = options.isStreamLive;
  }

  attemptStarted(): void {
    if (this.stopped) return;
    this.clearOpenTimer();
    this.openTimer = setTimeout(() => {
      this.openTimer = null;
      if (!this.stopped && this.isVisible()) this.reconnect();
    }, WORKBENCH_EVENT_OPEN_TIMEOUT_MS);
  }

  streamOpened(): void {
    if (this.stopped) return;
    this.retryAttempt = 0;
    this.clearRetryTimer();
    this.clearOpenTimer();
  }

  failed(): void {
    if (this.stopped) return;
    this.clearOpenTimer();
    if (this.retryTimer !== null || !this.isVisible()) return;
    const delayMs = Math.min(
      WORKBENCH_EVENT_RETRY_INITIAL_MS * (2 ** this.retryAttempt),
      WORKBENCH_EVENT_RETRY_MAX_MS,
    );
    if (delayMs < WORKBENCH_EVENT_RETRY_MAX_MS) this.retryAttempt += 1;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (!this.stopped && this.isVisible()) this.reconnect();
    }, delayMs);
  }

  /** The page came back or the network returned: stop waiting out a backoff. */
  wake(): void {
    if (this.stopped || !this.isVisible()) return;
    // A backoff was sized while nobody was watching. Someone is watching now,
    // so the next attempt should not sit out the rest of a 15s delay.
    this.retryAttempt = 0;
    this.clearRetryTimer();
    // An unbroken stream missed nothing, so there is no gap to close. Recycling
    // it would manufacture the very outage -- and the catch-up read every
    // consumer runs on reconnect -- that waking exists to recover from.
    // Checked before clearOpenTimer() so a stream that is open at the transport
    // but has not completed its handshake keeps its watchdog armed.
    if (this.isStreamLive()) return;
    this.clearOpenTimer();
    this.reconnect();
  }

  stop(): void {
    this.stopped = true;
    this.retryAttempt = 0;
    this.clearRetryTimer();
    this.clearOpenTimer();
  }

  private clearRetryTimer(): void {
    if (this.retryTimer === null) return;
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }

  private clearOpenTimer(): void {
    if (this.openTimer === null) return;
    clearTimeout(this.openTimer);
    this.openTimer = null;
  }
}
