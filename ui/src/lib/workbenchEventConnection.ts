export type WorkbenchEventConnectionState = 'connected' | 'reconnecting';

export const WORKBENCH_EVENT_RETRY_INITIAL_MS = 1_000;
export const WORKBENCH_EVENT_RETRY_MAX_MS = 15_000;
export const WORKBENCH_EVENT_OPEN_TIMEOUT_MS = 20_000;

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
