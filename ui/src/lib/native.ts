export type NativePlatform = 'ios' | 'android';
export type NativeCapability =
  | 'accountPresentation'
  | 'app'
  | 'badge'
  | 'health'
  | 'haptics'
  | 'instancePresentation'
  | 'keyboardResizesViewport'
  | 'notifications'
  | 'push'
  | 'systemSchedule'
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
/** iOS's notification permission for the App. Older shells don't say. */
export type NativePushPermission = 'granted' | 'notDetermined' | 'denied';
export type NativePushStatus =
  | {
      state: 'unavailable' | 'off';
      revokeState?: NativePushRevokeState;
      endpoint?: string;
      permission?: NativePushPermission;
    }
  | ({
      state: 'on';
      revokeState?: NativePushRevokeState;
      permission?: NativePushPermission;
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
  /** With `instancePresentation`: the selected instance's identity and display name. */
  instance?: { id?: string; name?: string };
  /** With `systemSchedule`: the stable identity of this App installation. */
  device?: { id?: string };
};

export type NativeSystemScheduleRequest = {
  requestId: string;
  operationId: string;
  kind: 'alarm' | 'reminder';
  title: string;
  fireAt: string;
  timeZone: string;
  recurrence: null;
  instanceId: string;
  sessionId: string;
  expiresAt: string;
};

export type NativeSystemScheduleCancel = Pick<
  NativeSystemScheduleRequest,
  'requestId' | 'operationId' | 'kind'
> & {
  purpose: 'user';
};

export type NativeSystemScheduleReceipt = {
  contract: 1;
  requestId: string;
  operationId: string;
  kind: 'alarm' | 'reminder';
  state:
    | 'scheduled'
    | 'user_denied'
    | 'permission_denied'
    | 'unsupported'
    | 'failed'
    | 'expired'
    | 'stale_context'
    | 'cancelled';
  systemIdentifier: string | null;
  fireAt: string | null;
  timeZone: string | null;
  authorization: 'not_determined' | 'authorized' | 'denied' | 'restricted' | 'unsupported';
  error: { code: string; message: string } | null;
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

/** The capability a shell advertises when it can present the system share sheet for a Show. */
export const NATIVE_SHARE_CAPABILITY = 'c1.share_draft';
export type NativeShareKind = 'link' | 'pdf' | 'image';
export type NativeShareStatus = 'completed' | 'cancelled' | 'failed' | 'expired';
/**
 * The terminal outcome of one `share.present`. `completed` means only that the system sheet
 * reported a successful handoff. A refusal the Web UI decides on its own (no capability, a probe
 * that does not offer the kind) reads as `failed` with `capability_mismatch`.
 */
export type NativeShareResult = {
  operationId: string;
  kind: NativeShareKind;
  status: NativeShareStatus;
  error?: { code: string };
};

const NATIVE_SHARE_STATUSES: readonly NativeShareStatus[] = ['completed', 'cancelled', 'failed', 'expired'];
const NATIVE_SHARE_MAX_SESSION_ID_LENGTH = 200;

/** Whether this shell can share a Show through `share.present` (any kind). */
export function hasNativeShare(): boolean {
  return hasNativeCapability(NATIVE_SHARE_CAPABILITY);
}

function newShareOperationId(): string | null {
  const crypto = globalThis.crypto;
  if (typeof crypto?.randomUUID === 'function') return crypto.randomUUID();
  if (typeof crypto?.getRandomValues !== 'function') return null;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function offersShareKind(capabilities: unknown, kind: NativeShareKind): boolean {
  if (!capabilities || typeof capabilities !== 'object') return false;
  const { version, outbound_kinds: outbound } = capabilities as {
    version?: unknown;
    outbound_kinds?: unknown;
  };
  return version === 1 && Array.isArray(outbound) && outbound.includes(kind);
}

function bridgeErrorCode(error: unknown, fallback: string): string {
  if (error && typeof error === 'object' && 'code' in error) {
    const { code } = error as { code?: unknown };
    if (typeof code === 'string' && code) return code;
  }
  return fallback;
}

/**
 * Asks the shell to share the Show of `sessionId` as its link, or as a PDF or image of the part of
 * its page the user sees now. The call names only the kind and the Session: the shell reads the link
 * and title from this instance's own Show APIs and captures the visible frame itself, so no URL,
 * title, geometry, or content crosses the bridge, and no permission changes. For `pdf` and `image`
 * the caller must have removed every overlay above the page first. Never throws.
 */
export async function nativeSharePresent(
  kind: NativeShareKind,
  sessionId: string,
): Promise<NativeShareResult> {
  const operationId = newShareOperationId() ?? '';
  const failed = (code: string): NativeShareResult => ({
    operationId,
    kind,
    status: 'failed',
    error: { code },
  });
  const native = bridge();
  if (!native || (native.capabilities[NATIVE_SHARE_CAPABILITY] ?? 0) < 1) {
    return failed('capability_mismatch');
  }
  if (!operationId) return failed('unavailable');
  if (!sessionId || sessionId.length > NATIVE_SHARE_MAX_SESSION_ID_LENGTH) {
    return failed('invalid_request');
  }
  try {
    const capabilities = await native.call<unknown>('share.capabilities');
    if (!offersShareKind(capabilities, kind)) return failed('capability_mismatch');
  } catch (error) {
    return failed(bridgeErrorCode(error, 'unavailable'));
  }
  let result: unknown;
  try {
    result = await native.call<unknown>('share.present', { operationId, kind, sessionId });
  } catch (error) {
    return failed(bridgeErrorCode(error, 'unavailable'));
  }
  if (!result || typeof result !== 'object') return failed('invalid_response');
  const { operationId: echoedId, kind: echoedKind, status, error } = result as {
    operationId?: unknown;
    kind?: unknown;
    status?: unknown;
    error?: unknown;
  };
  if (
    echoedId !== operationId
    || echoedKind !== kind
    || !NATIVE_SHARE_STATUSES.includes(status as NativeShareStatus)
  ) {
    return failed('invalid_response');
  }
  const shareStatus = status as NativeShareStatus;
  if (shareStatus === 'failed' || shareStatus === 'expired') {
    return {
      operationId,
      kind,
      status: shareStatus,
      error: { code: bridgeErrorCode(error, shareStatus) },
    };
  }
  return { operationId, kind, status: shareStatus };
}

export async function nativeAppInfo(): Promise<NativeAppInfo | null> {
  return callNative<NativeAppInfo>('app', 'app.info');
}

/** Establishes a connection only; request completion comes from the instance API. */
export async function nativeHealthConnect(params: { requestId: string; pairingCode: string }): Promise<{ state: 'connected' | 'declined' | 'failed' } | null> {
  return callNative('health', 'health.connect', params);
}

export async function nativeSystemScheduleRequest(
  request: NativeSystemScheduleRequest,
): Promise<NativeSystemScheduleReceipt | null> {
  return callNative<NativeSystemScheduleReceipt>(
    'systemSchedule',
    'systemSchedule.request',
    request,
  );
}

export async function nativeSystemScheduleCancel(
  request: NativeSystemScheduleCancel,
): Promise<NativeSystemScheduleReceipt | null> {
  return callNative<NativeSystemScheduleReceipt>(
    'systemSchedule',
    'systemSchedule.cancel',
    request,
  );
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

/**
 * Every in-App account entry (the header avatar, More's account, the account menu). Where the
 * header names the instance (`instancePresentation`), the shell's sheet opens on the Account page
 * (`#sheet-account`) and the name opens the instances; older shells open their account sheet as
 * before. False where the shell lacks account presentation or the call fails.
 */
export async function nativeShowAccount(): Promise<boolean> {
  try {
    if (!hasNativeCapability('accountPresentation')) return false;
    await callNative<null>(
      'accountPresentation',
      'app.showAccount',
      hasNativeCapability('instancePresentation') ? { start: 'account' } : {},
    );
    return true;
  } catch {
    return false;
  }
}

/**
 * Opens the shell's instances sheet over this page (`instancePresentation`): the instance list
 * without the account. False where the shell lacks the capability or the call fails.
 */
export async function nativeShowInstances(): Promise<boolean> {
  try {
    if (!hasNativeCapability('instancePresentation')) return false;
    await callNative<null>('instancePresentation', 'app.showInstances');
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

/** The most unread Sessions one `notifications.reconcile` may name (docs/plans/ios-app.md section 4). */
export const NATIVE_RECONCILE_MAX_SESSIONS = 1000;
const NATIVE_RECONCILE_MAX_ID_LENGTH = 200;

/**
 * Tells the native App which Sessions are unread in a whole unread map the
 * server returned, so it retires the delivered notifications of every other
 * Session. `asOf` is the device time just before the request that returned
 * `unreadBySession` was sent: the map describes the server at that moment or
 * later, so a notification delivered by then and absent from it was read.
 * Fire-and-forget: false where the shell lacks the capability, or when the map
 * cannot be described within the contract's bounds; then nothing is retired.
 */
export function reconcileNativeNotifications(
  unreadBySession: unknown,
  asOf: number,
): boolean {
  if (!hasNativeCapability('notifications')) return false;
  if (
    !unreadBySession
    || typeof unreadBySession !== 'object'
    || Array.isArray(unreadBySession)
    || !Number.isFinite(asOf)
  ) {
    return false;
  }
  const unreadSessions: string[] = [];
  for (const [sessionId, count] of Object.entries(unreadBySession)) {
    if (typeof count !== 'number' || !Number.isFinite(count)) return false;
    if (count <= 0) continue;
    // A Session the call could not name would read as read: send nothing.
    if (!sessionId || sessionId.length > NATIVE_RECONCILE_MAX_ID_LENGTH) return false;
    unreadSessions.push(sessionId);
  }
  if (unreadSessions.length > NATIVE_RECONCILE_MAX_SESSIONS) return false;
  void callNative<null>('notifications', 'notifications.reconcile', {
    unreadSessions,
    asOf,
  }).catch(() => {});
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
 * How long a baseline read may go unanswered before this document shows its
 * streams as unavailable. A proposed display timing for the UI, not a native
 * deadline: the shell may hold its answer longer (while its local proxy gets
 * ready after a resume), and a valid answer that arrives later still counts.
 */
const DIRECT_BASELINE_WAIT_MS = 1500;

/**
 * Whether this document may open its first SSE stream or terminal socket.
 * `absent`: no `direct` capability, so nothing to wait for. `pending`: a
 * baseline read is out. `ready`: a valid `direct.status()` answer was
 * received; only then may a first stream open, and the state never changes
 * again. `unavailable`: no valid answer yet (the read failed, or has not
 * answered within the wait); nothing opens until one arrives.
 */
export type NativeDirectBaselineState = 'absent' | 'pending' | 'ready' | 'unavailable';

type DirectTracker = {
  state: Exclude<NativeDirectBaselineState, 'absent'>;
  /** The highest closure handled after `ready`, or seen before it. */
  closure: number;
  /** Numbers baseline attempts; only the current one may change the state. */
  attempt: number;
  attemptTimer: number | null;
  /** Counts the times this document was hidden; one read per return. */
  hiddenCount: number;
  /** The `hiddenCount` whose return has already read the status. */
  readForHidden: number;
  closureHandlers: Set<() => void>;
  stateListeners: Set<() => void>;
  /** The latest preference, ordered by `sequence`; null before any. */
  preferred: NativeDirectPreference | null;
  preferredSequence: number;
  preferenceHandlers: Set<(preferred: NativeDirectPreference) => void>;
};

// One per document: a WebView document and each child window load their own
// copy of this module, so each takes its own baseline. Kept outside React so
// a remount cannot take a second one.
let directTracker: DirectTracker | null = null;

function notifyDirectState(tracker: DirectTracker): void {
  for (const listener of Array.from(tracker.stateListeners)) {
    if (tracker.stateListeners.has(listener)) listener();
  }
}

// Every valid answer and event comes here, whichever attempt or read it
// belongs to. Only `closure` drives anything: `preferred` (ordered by
// `sequence`) is what new connections try first, and nothing in the Web UI
// acts on it. Closure is compared only with closure, so order, duplicates,
// and late replies all converge on one action per higher value.
function observeDirectStatus(
  tracker: DirectTracker,
  status: NativeDirectStatus,
  fromRead: boolean,
): void {
  observeDirectPreference(tracker, status, fromRead);
  if (tracker.state !== 'ready') {
    // No stream of this document is open yet: a closure only raises the
    // baseline. A valid status answer is the baseline; an event is not.
    tracker.closure = Math.max(tracker.closure, status.closure);
    if (!fromRead) return;
    tracker.state = 'ready';
    if (tracker.attemptTimer !== null) window.clearTimeout(tracker.attemptTimer);
    tracker.attemptTimer = null;
    notifyDirectState(tracker);
    return;
  }
  if (status.closure <= tracker.closure) return;
  tracker.closure = status.closure;
  for (const handler of Array.from(tracker.closureHandlers)) {
    if (tracker.closureHandlers.has(handler)) handler();
  }
}

// The preference, ordered by `sequence`. Its handlers hear every change, and
// every status read while it is `direct` (a resume), so a hand-over skipped
// earlier can be tried again; they decide for themselves whether to act.
function observeDirectPreference(
  tracker: DirectTracker,
  status: NativeDirectStatus,
  fromRead: boolean,
): void {
  if (status.sequence <= tracker.preferredSequence && tracker.preferred !== null) {
    if (!(fromRead && tracker.preferred === 'direct')) return;
  } else {
    const changed = tracker.preferred !== status.preferred;
    tracker.preferred = status.preferred;
    tracker.preferredSequence = status.sequence;
    if (!changed && !fromRead) return;
  }
  const preferred = tracker.preferred;
  if (preferred === null) return;
  for (const handler of Array.from(tracker.preferenceHandlers)) {
    if (tracker.preferenceHandlers.has(handler)) handler(preferred);
  }
}

async function readDirectStatus(native: AvibeNative): Promise<NativeDirectStatus | null> {
  try {
    return parseNativeDirectStatus(await native.call<unknown>('direct.status'));
  } catch {
    return null;
  }
}

/**
 * Starts a baseline attempt, which becomes the current one. An older attempt
 * cannot be cancelled (the bridge has no way to), but it loses its timer and
 * its power to make the state `unavailable`. A valid answer from it still
 * counts, like any observation.
 */
function startBaselineAttempt(native: AvibeNative, tracker: DirectTracker): void {
  tracker.attempt += 1;
  const attempt = tracker.attempt;
  if (tracker.attemptTimer !== null) window.clearTimeout(tracker.attemptTimer);
  tracker.state = 'pending';
  tracker.attemptTimer = window.setTimeout(() => {
    if (tracker.attempt !== attempt || tracker.state !== 'pending') return;
    tracker.attemptTimer = null;
    tracker.state = 'unavailable';
    notifyDirectState(tracker);
  }, DIRECT_BASELINE_WAIT_MS);
  notifyDirectState(tracker);
  void readDirectStatus(native).then((status) => {
    if (status) {
      observeDirectStatus(tracker, status, true);
      return;
    }
    // A failed read ends only the current attempt, and never a `ready` one.
    if (tracker.attempt !== attempt || tracker.state !== 'pending') return;
    if (tracker.attemptTimer !== null) window.clearTimeout(tracker.attemptTimer);
    tracker.attemptTimer = null;
    tracker.state = 'unavailable';
    notifyDirectState(tracker);
  });
}

function directTrackerFor(native: AvibeNative): DirectTracker {
  if (directTracker) return directTracker;
  const tracker: DirectTracker = {
    state: 'pending',
    closure: 0,
    attempt: 0,
    attemptTimer: null,
    // A document installed while hidden has already had its hidden edge, so
    // its first visible return is a return like any other.
    hiddenCount: document.visibilityState === 'visible' ? 0 : 1,
    readForHidden: 0,
    closureHandlers: new Set(),
    stateListeners: new Set(),
    preferred: null,
    preferredSequence: -1,
    preferenceHandlers: new Set(),
  };
  directTracker = tracker;

  native.on('direct.transportChanged', (payload) => {
    const status = parseNativeDirectStatus(payload);
    if (status) observeDirectStatus(tracker, status, false);
  });

  // One read each time the document is shown again after being hidden.
  // `visibilitychange` and a back-forward-cache `pageshow` for the same return
  // share it. When `ready`, it catches up on a closure whose events were all
  // missed while the App was suspended (the shell holds the answer until its
  // local proxy is ready again). When `unavailable`, it is a new baseline
  // attempt. While an attempt is pending it adds nothing.
  const onReturn = () => {
    if (tracker.readForHidden === tracker.hiddenCount) return;
    tracker.readForHidden = tracker.hiddenCount;
    if (tracker.state === 'unavailable') {
      startBaselineAttempt(native, tracker);
    } else if (tracker.state === 'ready') {
      void readDirectStatus(native).then((status) => {
        if (status) observeDirectStatus(tracker, status, true);
      });
    }
  };
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') onReturn();
    else tracker.hiddenCount += 1;
  });
  window.addEventListener('pagehide', () => {
    tracker.hiddenCount += 1;
  });
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) onReturn();
  });

  startBaselineAttempt(native, tracker);
  return tracker;
}

function directBridge(): AvibeNative | null {
  const native = bridge();
  return native && (native.capabilities.direct ?? 0) >= 1 ? native : null;
}

/**
 * This document's baseline state. The first call with the `direct`
 * capability starts the baseline read; without it the answer is `absent` and
 * nothing is started.
 */
export function nativeDirectBaselineState(): NativeDirectBaselineState {
  const native = directBridge();
  return native ? directTrackerFor(native).state : 'absent';
}

/** Calls `listener` on every change of `nativeDirectBaselineState()`. */
export function subscribeNativeDirectBaseline(listener: () => void): () => void {
  const native = directBridge();
  if (!native) return () => undefined;
  const tracker = directTrackerFor(native);
  tracker.stateListeners.add(listener);
  return () => {
    tracker.stateListeners.delete(listener);
  };
}

/**
 * A user asked to try again while the baseline is `unavailable`: starts one
 * new attempt. Does nothing while an attempt is pending, once `ready`, or
 * without the capability.
 */
export function retryNativeDirectBaseline(): void {
  const native = directBridge();
  if (!native) return;
  const tracker = directTrackerFor(native);
  if (tracker.state === 'unavailable') startBaselineAttempt(native, tracker);
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

/**
 * Calls `handler` with the preference whenever it changes (ordered by
 * `sequence`), and on every status read while it is `direct`. Without the
 * `direct` capability this never fires.
 */
export function onNativeDirectPreference(
  handler: (preferred: NativeDirectPreference) => void,
): () => void {
  const native = directBridge();
  if (!native) return () => undefined;
  const tracker = directTrackerFor(native);
  tracker.preferenceHandlers.add(handler);
  return () => {
    tracker.preferenceHandlers.delete(handler);
  };
}

/** This document's latest preference, or null before any (or without `direct`). */
export function nativeDirectPreference(): NativeDirectPreference | null {
  const native = directBridge();
  return native ? directTrackerFor(native).preferred : null;
}

/** The shell answers `direct.handover` (`direct` version 2). */
export function nativeDirectHandoverAvailable(): boolean {
  return hasNativeCapability('direct', 2);
}

export type NativeDirectHandoverResult = { closed: number; kept: number; skipped?: string };

const DIRECT_HANDOVER_WAIT_MS = 5000;

/**
 * Asks the shell to close the quiet public connections for this host, so the
 * next connection takes the direct path (`direct.handover`). Null when
 * unsupported, failed, or unanswered within a few seconds.
 */
export async function nativeDirectHandover(): Promise<NativeDirectHandoverResult | null> {
  const native = directBridge();
  if (!native || !nativeDirectHandoverAvailable()) return null;
  let timer: number | null = null;
  try {
    const answer = await Promise.race([
      native.call<unknown>('direct.handover'),
      new Promise<null>((resolve) => {
        timer = window.setTimeout(() => resolve(null), DIRECT_HANDOVER_WAIT_MS);
      }),
    ]);
    if (!answer || typeof answer !== 'object') return null;
    const { closed, kept, skipped } = answer as { closed?: unknown; kept?: unknown; skipped?: unknown };
    if (!isCounter(closed) || !isCounter(kept)) return null;
    return typeof skipped === 'string' ? { closed, kept, skipped } : { closed, kept };
  } catch {
    return null;
  } finally {
    if (timer !== null) window.clearTimeout(timer);
  }
}

/** The kind of path the App's own direct connection runs on. */
export type NativeDirectPathKind = 'relay' | 'ipv4' | 'ipv6';

/** For the Settings line: the preference, and the App's own path if it has one. */
export type NativeDirectPathSnapshot = {
  preferred: NativeDirectPreference;
  path: NativeDirectPathKind | null;
};

/** Reads `direct.status()` once for display. Null without `direct` or on failure. */
export async function readNativeDirectPath(): Promise<NativeDirectPathSnapshot | null> {
  const native = directBridge();
  if (!native) return null;
  try {
    const answer = await native.call<unknown>('direct.status');
    const status = parseNativeDirectStatus(answer);
    if (!status) return null;
    const raw = (answer as { path?: unknown }).path;
    const path = raw === 'relay' || raw === 'ipv4' || raw === 'ipv6' ? raw : null;
    return { preferred: status.preferred, path };
  } catch {
    return null;
  }
}
