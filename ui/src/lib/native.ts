export type NativePlatform = 'ios' | 'android';
export type NativeCapability =
  | 'accountPresentation'
  | 'app'
  | 'badge'
  | 'haptics'
  | 'keyboardResizesViewport'
  | 'push'
  | 'viewport'
  | 'voice';
export type NativeImpactStyle = 'light' | 'medium' | 'heavy' | 'soft' | 'rigid';
export type NativeNotificationType = 'success' | 'warning' | 'error';
export type NativeVoicePermission = 'granted' | 'denied' | 'prompt';
export type NativePushSubscription = {
  endpoint: string;
  keys: { p256dh: string; auth: string };
};
export type NativePushRevokeState = 'active' | 'absent' | 'pending';
export type NativePushStatus =
  | {
      state: 'unavailable' | 'off';
      revokeState?: NativePushRevokeState;
      endpoint?: string;
    }
  | ({
      state: 'on';
      revokeState?: NativePushRevokeState;
    } & NativePushSubscription);

export type NativeDirectPreference = 'tunnel' | 'direct';
/**
 * `direct.status()` and `direct.transportChanged`, as specified in
 * docs/plans/ios-app.md section 4. `preferred` is what this document's new
 * connections try first, ordered by `sequence`. `closure` counts the times the
 * shell closed connections for this host because their path was lost; it only
 * grows, and each increase asks the Web UI to reconnect once.
 */
export type NativeDirectStatus = {
  preferred: NativeDirectPreference;
  sequence: number;
  closure: number;
};

export type NativeBridgeError = {
  code: string;
  message: string;
};

export type NativeAppInfo = {
  contract: 1;
  platform: NativePlatform;
  shellVersion: string;
  capabilities: Record<string, number>;
};

interface AvibeNative {
  readonly contract: 1;
  readonly platform: NativePlatform;
  readonly shellVersion: string;
  readonly capabilities: Readonly<Record<string, number>>;
  call<T>(method: string, params?: Record<string, unknown>): Promise<T>;
  on(event: string, handler: (payload: unknown) => void): () => void;
}

declare global {
  interface Window {
    /** Defined at document start by an Avibe native shell for a trusted main frame. */
    readonly avibeNative?: AvibeNative;
  }
}

function bridge(): AvibeNative | null {
  if (typeof window === 'undefined') return null;
  const candidate = window.avibeNative;
  if (
    candidate?.contract !== 1
    || (candidate.platform !== 'ios' && candidate.platform !== 'android')
    || typeof candidate.call !== 'function'
    || typeof candidate.on !== 'function'
    || !candidate.capabilities
  ) {
    return null;
  }
  return candidate;
}

export function isNativeShell(): boolean {
  return bridge() !== null;
}

export function hasNativeCapability(
  capability: string,
  minimumVersion = 1,
): boolean {
  const native = bridge();
  return Boolean(native && (native.capabilities[capability] ?? 0) >= minimumVersion);
}

async function callNative<T>(
  capability: NativeCapability,
  method: string,
  params: Record<string, unknown> = {},
): Promise<T | null> {
  const native = bridge();
  if (!native || (native.capabilities[capability] ?? 0) < 1) return null;
  return native.call<T>(method, params);
}

export async function nativeAppInfo(): Promise<NativeAppInfo | null> {
  return callNative<NativeAppInfo>('app', 'app.info');
}

export async function openNativeExternal(url: string): Promise<boolean> {
  try {
    const result = await callNative<{ opened: boolean }>(
      'app',
      'app.openExternal',
      { url },
    );
    return result?.opened === true;
  } catch {
    return false;
  }
}

export async function nativeShowAccount(): Promise<boolean> {
  try {
    if (!hasNativeCapability('accountPresentation')) return false;
    await callNative<null>('accountPresentation', 'app.showAccount');
    return true;
  } catch {
    return false;
  }
}

export async function setNativeAccountActionAvailable(
  available: boolean,
): Promise<boolean> {
  try {
    if (!hasNativeCapability('accountPresentation', 2)) return false;
    await callNative<null>(
      'accountPresentation',
      'app.setAccountActionAvailable',
      { available },
    );
    return true;
  } catch {
    return false;
  }
}

/**
 * Sets the native App's icon badge to `count`, the Inbox total; 0 clears it.
 * Fire-and-forget: true when the shell has the `badge` capability and the call
 * went out, false where the shell predates it (or there is no shell), which
 * leaves the badge to whatever else sets it.
 */
export function setNativeBadge(count: number): boolean {
  if (!hasNativeCapability('badge')) return false;
  const value = Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0;
  void callNative<null>('badge', 'badge.set', { count: value }).catch(() => {});
  return true;
}

export async function nativeHapticImpact(style: NativeImpactStyle): Promise<boolean> {
  try {
    if (!hasNativeCapability('haptics')) return false;
    await callNative<null>('haptics', 'haptics.impact', { style });
    return true;
  } catch {
    return false;
  }
}

export async function nativeHapticNotify(type: NativeNotificationType): Promise<boolean> {
  try {
    if (!hasNativeCapability('haptics')) return false;
    await callNative<null>('haptics', 'haptics.notify', { type });
    return true;
  } catch {
    return false;
  }
}

export async function nativeHapticSelection(): Promise<boolean> {
  try {
    if (!hasNativeCapability('haptics')) return false;
    await callNative<null>('haptics', 'haptics.selection');
    return true;
  } catch {
    return false;
  }
}

export async function setNativeOuterScrollLocked(locked: boolean): Promise<boolean> {
  try {
    if (!hasNativeCapability('viewport')) return false;
    await callNative<null>(
      'viewport',
      'viewport.setOuterScrollLocked',
      { locked },
    );
    return true;
  } catch {
    return false;
  }
}

export async function nativeVoicePermission(): Promise<NativeVoicePermission | null> {
  try {
    const result = await callNative<{ state: NativeVoicePermission }>(
      'voice',
      'voice.permission',
    );
    return result?.state ?? null;
  } catch {
    return null;
  }
}

export async function nativePushStatus(
  vapidPublicKey?: string,
): Promise<NativePushStatus | null> {
  const outcome = await nativePushStatusOutcome(vapidPublicKey);
  return outcome.status === 'ok' ? outcome.value : null;
}

export type NativePushStatusOutcome =
  | { status: 'ok'; value: NativePushStatus }
  | { status: 'failed' };

export async function nativePushStatusOutcome(
  vapidPublicKey?: string,
): Promise<NativePushStatusOutcome> {
  try {
    const value = await callNative<NativePushStatus>(
      'push',
      'push.status',
      vapidPublicKey ? { vapidPublicKey } : {},
    );
    return value ? { status: 'ok', value } : { status: 'failed' };
  } catch {
    return { status: 'failed' };
  }
}

export async function nativePushSubscribe(
  vapidPublicKey: string,
  forceResubscribe = false,
): Promise<NativePushSubscription | null> {
  try {
    return await callNative<NativePushSubscription>(
      'push',
      'push.subscribe',
      forceResubscribe
        ? { vapidPublicKey, forceResubscribe: true }
        : { vapidPublicKey },
    );
  } catch {
    return null;
  }
}

export async function nativePushUnsubscribe(): Promise<boolean> {
  const status = (await nativePushUnsubscribeOutcome()).status;
  return status === 'confirmed' || status === 'not_needed';
}

export type NativePushUnsubscribeOutcome =
  | { status: 'confirmed' }
  | { status: 'pending' }
  | { status: 'not_needed' }
  | { status: 'failed' };

/**
 * New shells include `revoke`; `revoked` keeps older Web bundles compatible.
 */
export async function nativePushUnsubscribeOutcome(): Promise<NativePushUnsubscribeOutcome> {
  try {
    const result = await callNative<{
      revoked: boolean;
      revoke?: 'confirmed' | 'pending' | 'not_needed';
    }>(
      'push',
      'push.unsubscribe',
    );
    if (!result) return { status: 'failed' };
    if (
      result.revoke === 'confirmed'
      || result.revoke === 'pending'
      || result.revoke === 'not_needed'
    ) {
      return { status: result.revoke };
    }
    return result.revoked ? { status: 'confirmed' } : { status: 'pending' };
  } catch {
    return { status: 'failed' };
  }
}

export function onNativePushOpened(
  handler: (path: string) => void,
): () => void {
  const native = bridge();
  if (!native || (native.capabilities.push ?? 0) < 1) return () => undefined;
  return native.on('push.opened', (payload) => {
    if (
      payload
      && typeof payload === 'object'
      && 'url' in payload
      && typeof payload.url === 'string'
    ) {
      try {
        const parsed = new URL(payload.url, window.location.origin);
        if (parsed.origin !== window.location.origin) return;
        handler(parsed.pathname + parsed.search + parsed.hash);
      } catch {
        // Ignore malformed navigation payloads from the native shell.
      }
    }
  });
}

function isCounter(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}

function parseNativeDirectStatus(payload: unknown): NativeDirectStatus | null {
  if (!payload || typeof payload !== 'object') return null;
  const { preferred, sequence, closure } = payload as {
    preferred?: unknown;
    sequence?: unknown;
    closure?: unknown;
  };
  if (preferred !== 'tunnel' && preferred !== 'direct') return null;
  if (!isCounter(sequence) || !isCounter(closure)) return null;
  return { preferred, sequence, closure };
}

/**
 * How long the first stream of this document waits for its `direct.status()`
 * baseline before it opens anyway. The shell answers at once outside a resume;
 * this only bounds a shell that never answers.
 */
const DIRECT_BASELINE_WAIT_MS = 1500;

type DirectTracker = {
  /** Settles once: the baseline is taken, or the read failed or timed out. */
  baseline: Promise<void>;
  settled: boolean;
  /** The highest closure this document has handled, or seen before its baseline. */
  closure: number;
  /** Counts the times this document was hidden; one read per return. */
  hiddenCount: number;
  /** The `hiddenCount` whose return has already read the status. */
  readForHidden: number;
  closureHandlers: Set<() => void>;
};

// One per document: a WebView document and each child window load their own
// copy of this module, so each takes its own baseline. Kept outside React so
// a remount cannot take a second one.
let directTracker: DirectTracker | null = null;

// Only `closure` drives anything here. `preferred` (ordered by `sequence`) is
// what new connections try first; nothing in the Web UI acts on it, so a
// change of it alone never reconnects anything.
function applyDirectStatus(tracker: DirectTracker, status: NativeDirectStatus): void {
  // Closure is compared only with closure, never with `sequence`, so it is
  // acted on whatever order, duplicates, or late replies bring. Before the
  // baseline it only raises the baseline: no stream of this document is open.
  if (status.closure <= tracker.closure) return;
  tracker.closure = status.closure;
  if (!tracker.settled) return;
  for (const handler of Array.from(tracker.closureHandlers)) {
    if (tracker.closureHandlers.has(handler)) handler();
  }
}

async function readDirectStatus(native: AvibeNative): Promise<NativeDirectStatus | null> {
  try {
    return parseNativeDirectStatus(await native.call<unknown>('direct.status'));
  } catch {
    return null;
  }
}

function directTrackerFor(native: AvibeNative): DirectTracker {
  if (directTracker) return directTracker;
  let settle = () => {};
  const tracker: DirectTracker = {
    baseline: new Promise<void>((resolve) => {
      settle = resolve;
    }),
    settled: false,
    closure: 0,
    hiddenCount: 0,
    readForHidden: 0,
    closureHandlers: new Set(),
  };
  directTracker = tracker;
  const finishBaseline = () => {
    if (tracker.settled) return;
    tracker.settled = true;
    settle();
  };

  native.on('direct.transportChanged', (payload) => {
    const status = parseNativeDirectStatus(payload);
    if (status) applyDirectStatus(tracker, status);
  });

  // The baseline is the first status answer. If the read fails, or has not
  // answered within the wait, the first streams open without one: nothing is
  // made up, and the watermark keeps only what was observed (the highest
  // closure an event showed meanwhile, else the counter's start, 0). The cost
  // is at most one extra reconnect for a closure from before this document;
  // a closure is never missed. An answer after the wait is applied like any
  // other observation. This bounded wait is this module's own policy: the
  // bridge defines no timeout.
  const wait = window.setTimeout(finishBaseline, DIRECT_BASELINE_WAIT_MS);
  void readDirectStatus(native).then((status) => {
    if (status) applyDirectStatus(tracker, status);
    window.clearTimeout(wait);
    finishBaseline();
  });

  // Resume: one read each time the document is shown again after being
  // hidden, so a closure whose events were all missed while the App was
  // suspended is still acted on. The shell holds this answer until its local
  // proxy is ready again. `visibilitychange` and a back-forward-cache
  // `pageshow` for the same return share one read. A read that never answers
  // does not hold up the next return, which reads again, and any answer that
  // does arrive, early or late, goes through the same closure watermark.
  const reconcile = () => {
    if (!tracker.settled || tracker.readForHidden === tracker.hiddenCount) return;
    tracker.readForHidden = tracker.hiddenCount;
    void readDirectStatus(native).then((status) => {
      if (status) applyDirectStatus(tracker, status);
    });
  };
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') reconcile();
    else tracker.hiddenCount += 1;
  });
  window.addEventListener('pagehide', () => {
    tracker.hiddenCount += 1;
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) reconcile();
  });
  return tracker;
}

function directBridge(): AvibeNative | null {
  const native = bridge();
  return native && (native.capabilities.direct ?? 0) >= 1 ? native : null;
}

/**
 * Resolves once this document has its `direct.status()` baseline. A long-lived
 * stream waits for it before it first opens, so a closure that happened
 * before the stream existed is never mistaken for one it should act on.
 * `null` when there is nothing to wait for: no `direct` capability, or the
 * baseline is already taken.
 */
export function nativeDirectBaseline(): Promise<void> | null {
  const native = directBridge();
  if (!native) return null;
  const tracker = directTrackerFor(native);
  return tracker.settled ? null : tracker.baseline;
}

/**
 * Calls `handler` once for each higher `closure` this document observes after
 * its baseline, from an event or from a status read on resume: the shell
 * closed connections for this host because their path was lost. The handler
 * reconnects only what reconnects without repeating an action. A change of
 * the preferred path alone never calls it. Without the `direct` capability
 * this never fires.
 */
export function onNativeDirectClosure(handler: () => void): () => void {
  const native = directBridge();
  if (!native) return () => undefined;
  const tracker = directTrackerFor(native);
  tracker.closureHandlers.add(handler);
  return () => {
    tracker.closureHandlers.delete(handler);
  };
}
