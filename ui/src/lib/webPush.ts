import type {
  ApiContextType,
  WebPushStatus,
  WebPushStatusPayload,
  WebPushSubscribeIntent,
  WebPushSubscriptionResult,
} from '@/context/ApiContext';
import { isIosDevice, isStandalonePwa } from './platform';
import { nativeDeviceLabel } from './deviceReport';
import {
  hasNativeCapability,
  nativePushStatus,
  nativePushStatusOutcome,
  nativePushSubscribe,
  nativePushUnsubscribe,
  nativePushUnsubscribeOutcome,
} from './native';

const WEB_PUSH_DEVICE_ID_KEY = 'vibe.webPush.deviceId';
const WEB_PUSH_ENDPOINTS_KEY = 'vibe.webPush.endpoints';
const NATIVE_PUSH_DISABLED_INTENT_KEY = 'vibe.nativePush.disabledIntent';
const WEB_PUSH_ENDPOINT_CACHE = 'avibe.web-push-endpoint.v1';
const WEB_PUSH_ENDPOINT_ENTRY_PATH = '/__avibe/web-push-endpoint';
const WEB_PUSH_DEVICE_ENTRY_PATH = '/__avibe/web-push-device-id';
const NATIVE_PUSH_PENDING_REGISTRATION_KEY = 'vibe.nativePush.pendingRegistration';
const NATIVE_PUSH_INTENT_STATE_KEY = 'vibe.nativePush.intentState';
const NATIVE_PUSH_LIFECYCLE_LOCK_KEY = 'vibe.nativePush.lifecycle';
const NATIVE_PUSH_LOCK_LEASE_MS = 30_000;

export type WebPushSupportState =
  | { supported: true; standalone: boolean; requiresStandalone: boolean }
  | { supported: false; reason: 'unsupported' | 'ios_requires_standalone' };

function urlBase64ToArrayBuffer(value: string): ArrayBuffer {
  const padding = '='.repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
  return output.buffer;
}

function arrayBuffersEqual(left: ArrayBuffer | null, right: ArrayBuffer): boolean {
  if (!left || left.byteLength !== right.byteLength) return false;
  const leftView = new Uint8Array(left);
  const rightView = new Uint8Array(right);
  for (let i = 0; i < leftView.length; i += 1) {
    if (leftView[i] !== rightView[i]) return false;
  }
  return true;
}

async function readCachedPushValue(path: string, key: 'endpoint' | 'device_id'): Promise<string | null> {
  if (!('caches' in window)) return null;
  try {
    const cache = await window.caches.open(WEB_PUSH_ENDPOINT_CACHE);
    const response = await cache.match(new URL(path, window.location.origin).href);
    const payload = response ? await response.json() : null;
    const value = payload?.[key];
    return typeof value === 'string' && value ? value : null;
  } catch {
    return null;
  }
}

async function writeCachedPushValue(path: string, key: 'endpoint' | 'device_id', value: string): Promise<void> {
  if (!('caches' in window)) return;
  try {
    const cache = await window.caches.open(WEB_PUSH_ENDPOINT_CACHE);
    await cache.put(new URL(path, window.location.origin).href, new Response(JSON.stringify({ [key]: value }), {
      headers: { 'content-type': 'application/json' },
    }));
  } catch {
    // localStorage remains available where Cache Storage is blocked.
  }
}

let deviceIdPromise: Promise<string> | undefined;

export function getWebPushDeviceId(): Promise<string> {
  const resolveDeviceId = async () => {
    let deviceId: string | null = null;
    try {
      deviceId = window.localStorage.getItem(WEB_PUSH_DEVICE_ID_KEY);
    } catch {
      // Hardened browsers can block localStorage while allowing Cache Storage.
    }
    deviceId ||= await readCachedPushValue(WEB_PUSH_DEVICE_ENTRY_PATH, 'device_id');
    deviceId ||= window.crypto?.randomUUID?.()
      ?? `device-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    try {
      window.localStorage.setItem(WEB_PUSH_DEVICE_ID_KEY, deviceId);
    } catch {
      // Cache Storage or this page's in-memory identity remains available.
    }
    await writeCachedPushValue(WEB_PUSH_DEVICE_ENTRY_PATH, 'device_id', deviceId);
    return deviceId;
  };
  deviceIdPromise ??= (async () => (
    navigator.locks?.request
      ? navigator.locks.request(WEB_PUSH_DEVICE_ID_KEY, resolveDeviceId)
      : resolveDeviceId()
  ))();
  return deviceIdPromise;
}

export async function getRememberedWebPushEndpoints(): Promise<string[]> {
  let localEndpoints: string[] = [];
  try {
    const raw = window.localStorage.getItem(WEB_PUSH_ENDPOINTS_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (Array.isArray(parsed)) {
      localEndpoints = parsed.filter((endpoint): endpoint is string => typeof endpoint === 'string' && endpoint.length > 0);
    }
  } catch {
    // Cache Storage remains available where localStorage is blocked.
  }
  const cachedEndpoint = await readCachedPushValue(WEB_PUSH_ENDPOINT_ENTRY_PATH, 'endpoint');
  return [...new Set([...(cachedEndpoint ? [cachedEndpoint] : []), ...localEndpoints])].slice(0, 8);
}

export async function rememberWebPushEndpoint(endpoint: string | undefined): Promise<void> {
  if (!endpoint) return;
  const endpoints = [endpoint, ...(await getRememberedWebPushEndpoints()).filter((candidate) => candidate !== endpoint)].slice(0, 8);
  try {
    window.localStorage.setItem(WEB_PUSH_ENDPOINTS_KEY, JSON.stringify(endpoints));
  } catch {
    // Best-effort persistence only.
  }
  await writeCachedPushValue(WEB_PUSH_ENDPOINT_ENTRY_PATH, 'endpoint', endpoint);
}

export function getWebPushSupportState(): WebPushSupportState {
  if (typeof window === 'undefined' || typeof navigator === 'undefined') {
    return { supported: false, reason: 'unsupported' };
  }
  if (hasNativeCapability('push')) {
    return { supported: true, standalone: true, requiresStandalone: false };
  }
  const hasApis = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  if (!hasApis) return { supported: false, reason: 'unsupported' };
  const standalone = isStandalonePwa();
  if (isIosDevice() && !standalone) {
    return { supported: false, reason: 'ios_requires_standalone' };
  }
  return { supported: true, standalone, requiresStandalone: isIosDevice() };
}

export async function getExistingWebPushSubscription(): Promise<PushSubscription | null> {
  if (!('serviceWorker' in navigator)) return null;
  const registration = await navigator.serviceWorker.getRegistration('/push-sw.js');
  return registration?.pushManager.getSubscription() ?? null;
}

export function webPushSubscriptionUsesVapidKey(
  subscription: PushSubscription,
  publicKey: string,
): boolean {
  try {
    return arrayBuffersEqual(
      subscription.options.applicationServerKey,
      urlBase64ToArrayBuffer(publicKey),
    );
  } catch {
    return false;
  }
}

/**
 * `registering`: the native shell holds a subscription the instance has not
 * accepted yet, and the last attempt to register it failed for a reason that can
 * pass (offline, a 5xx). It is not `enabled`: a test or a notification has no
 * instance row to reach this device through until a later reconcile posts it.
 */
export type WebPushReconcileStatus =
  | 'unsupported'
  | 'needs_install'
  | 'disabled'
  | 'disable_pending'
  | 'disable_failed'
  | 'registering'
  | 'enabled';

export type WebPushReconcileResult = {
  support: WebPushSupportState;
  status: WebPushReconcileStatus;
};

export type EnableWebPushOptions = {
  forceResubscribe?: boolean;
  recoverOnly?: boolean;
  pendingRegistrationStore?: NativePushPendingRegistrationStore;
  disabledIntentStore?: NativePushDisabledIntentStore;
};

export type NativePushPendingRegistration = {
  endpoint: string;
  keys: { p256dh: string; auth: string };
  previousEndpoints: string[];
  vapidPublicKey: string;
  /**
   * Why it was first posted, kept so that a retry says the same. Absent on a registration
   * written before this was recorded: a retry of unknown origin is never a press.
   */
  intent?: WebPushSubscribeIntent;
};

export interface NativePushPendingRegistrationStore {
  read(): NativePushPendingRegistration | null;
  write(registration: NativePushPendingRegistration): void;
  clear(): void;
}

/**
 * This lives in localStorage because the native shell gives each account its
 * own WKWebsiteDataStore, while localStorage is already scoped to that store
 * and the current instance origin.
 */
export type NativePushInstanceCleanupState =
  | 'unknown'
  | 'pending'
  | 'failed'
  | 'confirmed';
export type NativePushRevokeCleanupState =
  | 'unknown'
  | 'pending'
  | 'failed'
  | 'confirmed'
  | 'not_needed';
export type NativePushDisablePhase = 'capture_pending' | 'cleanup' | 'legacy';
export type NativePushIntentState = {
  revision: number;
  userIntent: 'enabled' | 'disabled';
};
export type NativePushDisableRecord = {
  version: 3;
  intentRevision: number;
  phase: NativePushDisablePhase;
  userIntent: 'disabled';
  endpoint: string | null;
  instanceCleanup: NativePushInstanceCleanupState;
  nativeRevoke: NativePushRevokeCleanupState;
};

export interface NativePushDisabledIntentStore {
  isDisabled(): boolean;
  read(): NativePushDisableRecord | null;
  readIntent(): NativePushIntentState;
  isCurrentIntentRevision(revision: number): boolean;
  beginDisable(): {
    record: NativePushDisableRecord;
    created: boolean;
    revision: number;
  };
  beginEnable(): { revision: number };
  update(
    patch: Partial<Pick<
      NativePushDisableRecord,
      'endpoint' | 'phase' | 'instanceCleanup' | 'nativeRevoke'
    >>,
  ): NativePushDisableRecord;
  clear(): void;
}

export function createNativePushDisabledIntentStore(
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
): NativePushDisabledIntentStore {
  const failClosedRecord = (
    intentRevision = 0,
    phase: NativePushDisablePhase = 'legacy',
  ): NativePushDisableRecord => ({
    version: 3,
    intentRevision,
    phase,
    userIntent: 'disabled',
    endpoint: null,
    instanceCleanup: 'unknown',
    nativeRevoke: 'unknown',
  });
  const isInstanceCleanupState = (
    value: unknown,
  ): value is NativePushInstanceCleanupState => (
    value === 'unknown'
    || value === 'pending'
    || value === 'failed'
    || value === 'confirmed'
  );
  const isNativeRevokeState = (
    value: unknown,
  ): value is NativePushRevokeCleanupState => (
    isInstanceCleanupState(value) || value === 'not_needed'
  );
  const isDisablePhase = (
    value: unknown,
  ): value is NativePushDisablePhase => (
    value === 'capture_pending'
    || value === 'cleanup'
    || value === 'legacy'
  );
  type StorageRead = {
    available: boolean;
    value: string | null;
  };
  const readStorageItem = (key: string): StorageRead => {
    try {
      return { available: true, value: storage.getItem(key) };
    } catch {
      return { available: false, value: null };
    }
  };
  type IntentRead = {
    available: boolean;
    intent: NativePushIntentState | null;
  };
  const readPersistedIntent = (): IntentRead => {
    const read = readStorageItem(NATIVE_PUSH_INTENT_STATE_KEY);
    if (!read.available) return { available: false, intent: null };
    const raw = read.value;
    if (raw === null) return { available: true, intent: null };
    try {
      const parsed = JSON.parse(raw) as {
        version?: unknown;
        revision?: unknown;
        userIntent?: unknown;
      };
      if (
        parsed.version !== 1
        || !Number.isSafeInteger(parsed.revision)
        || (parsed.revision as number) < 0
        || (parsed.userIntent !== 'enabled' && parsed.userIntent !== 'disabled')
      ) {
        return { available: true, intent: null };
      }
      return {
        available: true,
        intent: {
          revision: parsed.revision as number,
          userIntent: parsed.userIntent,
        },
      };
    } catch {
      return { available: true, intent: null };
    }
  };
  type RecordRead = {
    available: boolean;
    record: Record<string, unknown> | null;
  };
  const readRawDisableRecord = (): RecordRead => {
    const read = readStorageItem(NATIVE_PUSH_DISABLED_INTENT_KEY);
    if (!read.available) return { available: false, record: null };
    const raw = read.value;
    if (raw === null) return { available: true, record: null };
    try {
      const parsed = JSON.parse(raw);
      return {
        available: true,
        record: parsed && typeof parsed === 'object'
          ? parsed as Record<string, unknown>
          : {},
      };
    } catch {
      return { available: true, record: {} };
    }
  };
  const rawRecordIntentRevision = (
    parsed: Record<string, unknown> | null,
  ): number | null => (
    parsed && Number.isSafeInteger(parsed.intentRevision)
      && (parsed.intentRevision as number) >= 0
      ? parsed.intentRevision as number
      : null
  );
  const readIntentResult = (): IntentRead => {
    const persisted = readPersistedIntent();
    if (!persisted.available) {
      return {
        available: false,
        intent: { revision: 0, userIntent: 'disabled' },
      };
    }
    if (persisted.intent) return persisted;
    const rawResult = readRawDisableRecord();
    if (!rawResult.available) {
      return {
        available: false,
        intent: { revision: 0, userIntent: 'disabled' },
      };
    }
    const raw = rawResult.record;
    const revision = rawRecordIntentRevision(raw) ?? 0;
    return {
      available: true,
      intent: raw === null
        ? { revision: 0, userIntent: 'enabled' }
        : { revision, userIntent: 'disabled' },
    };
  };
  const readIntent = (): NativePushIntentState => (
    readIntentResult().intent ?? { revision: 0, userIntent: 'disabled' }
  );
  const readIntentForWrite = (): NativePushIntentState | null => (
    readIntentResult().available ? readIntent() : null
  );
  const readRecord = (): NativePushDisableRecord | null => {
    const intentResult = readIntentResult();
    const intent = intentResult.intent ?? {
      revision: 0,
      userIntent: 'disabled' as const,
    };
    if (intent.userIntent !== 'disabled') return null;
    const rawResult = readRawDisableRecord();
    if (!intentResult.available || !rawResult.available) {
      return failClosedRecord(intent.revision);
    }
    if (rawResult.record === null) {
      return failClosedRecord(intent.revision, 'capture_pending');
    }
    const record = normalizeRecord(rawResult.record, intent);
    const recordRevision = rawRecordIntentRevision(rawResult.record);
    if (recordRevision !== null && recordRevision !== intent.revision) {
      return null;
    }
    return {
      ...record,
      intentRevision: intent.revision,
    };
  };
  const writeIntent = (intent: NativePushIntentState): void => {
    storage.setItem(
      NATIVE_PUSH_INTENT_STATE_KEY,
      JSON.stringify({
        version: 1,
        revision: intent.revision,
        userIntent: intent.userIntent,
      }),
    );
    const persisted = readPersistedIntent().intent;
    if (
      !persisted
      || persisted.revision !== intent.revision
      || persisted.userIntent !== intent.userIntent
    ) {
      throw new Error('native_push_intent_persistence_failed');
    }
  };
  const normalizeRecord = (
    parsed: Record<string, unknown>,
    intent: NativePushIntentState,
  ): NativePushDisableRecord => {
    const endpoint = typeof parsed.endpoint === 'string' && parsed.endpoint.length > 0
      ? parsed.endpoint
      : null;
    if (
      parsed.version === 3
      && parsed.userIntent === 'disabled'
      && Number.isSafeInteger(parsed.intentRevision)
      && (parsed.intentRevision as number) >= 0
      && isDisablePhase(parsed.phase)
      && isInstanceCleanupState(parsed.instanceCleanup)
      && isNativeRevokeState(parsed.nativeRevoke)
    ) {
      return {
        version: 3,
        intentRevision: parsed.intentRevision as number,
        phase: parsed.phase,
        userIntent: 'disabled',
        endpoint,
        instanceCleanup: parsed.instanceCleanup,
        nativeRevoke: parsed.nativeRevoke,
      };
    }
    if (
      parsed.version === 2
      && parsed.userIntent === 'disabled'
      && isInstanceCleanupState(parsed.instanceCleanup)
      && isNativeRevokeState(parsed.nativeRevoke)
    ) {
      return {
        version: 3,
        intentRevision: intent.revision,
        phase: endpoint ? 'cleanup' : 'legacy',
        userIntent: 'disabled',
        endpoint,
        instanceCleanup: parsed.instanceCleanup,
        nativeRevoke: parsed.nativeRevoke,
      };
    }
    if (
      parsed.cleanup === 'pending'
      || parsed.cleanup === 'failed'
      || parsed.cleanup === 'confirmed'
    ) {
      return {
        ...failClosedRecord(intent.revision),
        endpoint,
      };
    }
    return failClosedRecord(intent.revision);
  };
  const write = (record: NativePushDisableRecord): NativePushDisableRecord => {
    storage.setItem(
      NATIVE_PUSH_DISABLED_INTENT_KEY,
      JSON.stringify(record),
    );
    const persisted = readRecord();
    if (
      !persisted
      || persisted.version !== record.version
      || persisted.intentRevision !== record.intentRevision
      || persisted.phase !== record.phase
      || persisted.userIntent !== record.userIntent
      || persisted.endpoint !== record.endpoint
      || persisted.instanceCleanup !== record.instanceCleanup
      || persisted.nativeRevoke !== record.nativeRevoke
    ) {
      throw new Error('native_push_disable_persistence_failed');
    }
    return persisted;
  };
  return {
    isDisabled: () => readRecord() !== null,
    read: readRecord,
    readIntent,
    isCurrentIntentRevision: (revision) => {
      const result = readIntentResult();
      return result.available && result.intent?.revision === revision;
    },
    beginDisable: () => {
      const intent = readIntentForWrite();
      if (!intent) {
        throw new Error('native_push_intent_persistence_failed');
      }
      const existing = readRecord();
      if (existing) {
        return {
          record: existing,
          created: false,
          revision: existing.intentRevision,
        };
      }
      const revision = intent.userIntent === 'disabled'
        ? intent.revision
        : intent.revision >= Number.MAX_SAFE_INTEGER
          ? (() => { throw new Error('native_push_intent_revision_exhausted'); })()
          : intent.revision + 1;
      if (intent.userIntent !== 'disabled') {
        writeIntent({
          revision,
          userIntent: 'disabled',
        });
      }
      const record = write(failClosedRecord(revision, 'capture_pending'));
      return { record, created: true, revision };
    },
    beginEnable: () => {
      const intent = readIntentForWrite();
      if (!intent) {
        throw new Error('native_push_intent_persistence_failed');
      }
      if (intent.revision >= Number.MAX_SAFE_INTEGER) {
        throw new Error('native_push_intent_revision_exhausted');
      }
      const revision = intent.revision + 1;
      writeIntent({ revision, userIntent: 'enabled' });
      storage.removeItem(NATIVE_PUSH_DISABLED_INTENT_KEY);
      return { revision };
    },
    update: (patch) => {
      const intent = readIntentForWrite();
      if (!intent) {
        throw new Error('native_push_intent_persistence_failed');
      }
      if (intent.userIntent !== 'disabled') {
        throw new Error('native_push_stale_intent');
      }
      const existing = readRecord();
      if (!existing) {
        throw new Error('native_push_disable_persistence_failed');
      }
      if (
        existing.endpoint
        && patch.endpoint !== undefined
        && patch.endpoint !== existing.endpoint
      ) {
        throw new Error('native_push_cleanup_endpoint_immutable');
      }
      const phase = patch.phase
        ?? (patch.endpoint && existing.phase === 'capture_pending'
          ? 'cleanup'
          : existing.phase);
      return write({
        ...existing,
        ...patch,
        phase,
        endpoint: existing.endpoint ?? patch.endpoint ?? null,
        version: 3,
        intentRevision: intent.revision,
        userIntent: 'disabled',
      });
    },
    clear: () => {
      const intent = readIntentForWrite();
      if (!intent) {
        throw new Error('native_push_enable_persistence_failed');
      }
      if (intent.revision >= Number.MAX_SAFE_INTEGER) {
        throw new Error('native_push_enable_persistence_failed');
      }
      const revision = intent.revision + 1;
      try {
        writeIntent({ revision, userIntent: 'enabled' });
        storage.removeItem(NATIVE_PUSH_DISABLED_INTENT_KEY);
        if (readIntent().revision !== revision) {
          throw new Error('native_push_enable_persistence_failed');
        }
      } catch {
        throw new Error('native_push_enable_persistence_failed');
      }
    },
  };
}

type NativePushIntentContext = {
  store: NativePushDisabledIntentStore;
  revision: number;
};

function assertNativePushIntentCurrent(
  context: NativePushIntentContext,
): void {
  if (!context.store.isCurrentIntentRevision(context.revision)) {
    throw new Error('native_push_stale_intent');
  }
}

let nativePushFallbackLifecycleQueue: Promise<unknown> = Promise.resolve();

function enqueueNativePushFallbackLifecycleOperation<T>(
  operation: () => Promise<T>,
): Promise<T> {
  const next = nativePushFallbackLifecycleQueue.then(operation, operation);
  nativePushFallbackLifecycleQueue = next.then(
    () => undefined,
    () => undefined,
  );
  return next;
}

function nativePushLockOwner(): string {
  return window.crypto?.randomUUID?.()
    ?? `native-push-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

async function withNativePushLease<T>(
  operation: () => Promise<T>,
): Promise<T> {
  let storage: Storage;
  try {
    storage = window.localStorage;
  } catch {
    return enqueueNativePushFallbackLifecycleOperation(operation);
  }

  const owner = nativePushLockOwner();
  const lockKey = NATIVE_PUSH_LIFECYCLE_LOCK_KEY;
  const wait = () => new Promise<void>((resolve) => {
    globalThis.setTimeout(resolve, 10);
  });
  for (;;) {
    const now = Date.now();
    let current: { owner: string; expiresAt: number } | null = null;
    try {
      const raw = storage.getItem(lockKey);
      const parsed = raw ? JSON.parse(raw) : null;
      if (
        parsed
        && typeof parsed.owner === 'string'
        && Number.isFinite(parsed.expiresAt)
      ) {
        current = parsed;
      }
    } catch {
      current = null;
    }
    if (!current || current.expiresAt <= now) {
      let acquired: boolean;
      try {
        storage.setItem(
          lockKey,
          JSON.stringify({
            owner,
            expiresAt: now + NATIVE_PUSH_LOCK_LEASE_MS,
          }),
        );
        const persisted = storage.getItem(lockKey);
        if (persisted === null) {
          throw new Error('native_push_lifecycle_lock_unavailable');
        }
        acquired = JSON.parse(persisted).owner === owner;
      } catch {
        return enqueueNativePushFallbackLifecycleOperation(operation);
      }
      if (acquired) {
        const heartbeat = globalThis.setInterval(() => {
          try {
            const held = JSON.parse(storage.getItem(lockKey) ?? '{}');
            if (held.owner === owner) {
              storage.setItem(
                lockKey,
                JSON.stringify({
                  owner,
                  expiresAt: Date.now() + NATIVE_PUSH_LOCK_LEASE_MS,
                }),
              );
            }
          } catch {
            // A stale lease will expire on its own.
          }
        }, Math.floor(NATIVE_PUSH_LOCK_LEASE_MS / 3));
        try {
          return await operation();
        } finally {
          globalThis.clearInterval(heartbeat);
          try {
            const held = JSON.parse(storage.getItem(lockKey) ?? '{}');
            if (held.owner === owner) storage.removeItem(lockKey);
          } catch {
            // A stale lease will expire on its own.
          }
        }
      }
    }
    await wait();
  }
}

function withNativePushLifecycleLock<T>(
  operation: () => Promise<T>,
): Promise<T> {
  if (typeof navigator !== 'undefined' && navigator.locks?.request) {
    try {
      type LockOutcome = { ok: true; value: T } | { ok: false; error: unknown };
      return navigator.locks.request<Promise<LockOutcome>>(
        NATIVE_PUSH_LIFECYCLE_LOCK_KEY,
        { mode: 'exclusive' },
        () => operation().then(
          (value) => ({ ok: true as const, value }),
          (error) => ({ ok: false as const, error }),
        ),
      ).then((outcome) => {
        if (!outcome.ok) throw outcome.error;
        return outcome.value;
      }, () => withNativePushLease(operation));
    } catch {
      // Fall through to the WebKit storage lease.
    }
  }
  return withNativePushLease(operation);
}

const browserNativePushPendingRegistrationStore: NativePushPendingRegistrationStore = {
  read: () => {
    try {
      const raw = window.localStorage.getItem(NATIVE_PUSH_PENDING_REGISTRATION_KEY);
      const parsed = raw ? JSON.parse(raw) : null;
      if (
        !parsed
        || typeof parsed.endpoint !== 'string'
        || typeof parsed.vapidPublicKey !== 'string'
        || typeof parsed.keys?.p256dh !== 'string'
        || typeof parsed.keys?.auth !== 'string'
        || !Array.isArray(parsed.previousEndpoints)
        || parsed.previousEndpoints.some((endpoint: unknown) => typeof endpoint !== 'string')
      ) {
        return null;
      }
      return {
        endpoint: parsed.endpoint,
        keys: {
          p256dh: parsed.keys.p256dh,
          auth: parsed.keys.auth,
        },
        previousEndpoints: parsed.previousEndpoints,
        vapidPublicKey: parsed.vapidPublicKey,
        ...(parsed.intent === 'explicit_enable' || parsed.intent === 'binding_repair'
          ? { intent: parsed.intent as WebPushSubscribeIntent }
          : {}),
      };
    } catch {
      return null;
    }
  },
  write: (registration) => {
    window.localStorage.setItem(
      NATIVE_PUSH_PENDING_REGISTRATION_KEY,
      JSON.stringify(registration),
    );
  },
  clear: () => {
    window.localStorage.removeItem(NATIVE_PUSH_PENDING_REGISTRATION_KEY);
  },
};

function getBrowserNativePushDisabledIntentStore(): NativePushDisabledIntentStore {
  return createNativePushDisabledIntentStore(window.localStorage);
}

/** Posts a subscription, naming an intent only when there is one. */
function postWebPushSubscription(
  api: ApiContextType,
  subscription: PushSubscriptionJSON,
  deviceLabel: string | undefined,
  deviceId: string | undefined,
  previousEndpoints: string[] | undefined,
  backgroundRotation: boolean | undefined,
  intent: WebPushSubscribeIntent | undefined,
): Promise<WebPushSubscriptionResult> {
  return intent
    ? api.subscribeWebPush(subscription, deviceLabel, deviceId, previousEndpoints, backgroundRotation, intent)
    : api.subscribeWebPush(subscription, deviceLabel, deviceId, previousEndpoints, backgroundRotation);
}

async function postNativePushRegistration(
  api: ApiContextType,
  registration: NativePushPendingRegistration,
  store: NativePushPendingRegistrationStore,
  recoverOnly?: boolean,
  disabledIntentStore?: NativePushDisabledIntentStore,
  intent?: NativePushIntentContext,
): Promise<void> {
  await withNativePushLifecycleLock(async () => {
    if (intent) {
      if (!intent.store.isCurrentIntentRevision(intent.revision)) {
        await nativePushUnsubscribe();
        store.clear();
        throw new Error('native_push_stale_intent');
      }
    } else if (disabledIntentStore?.isDisabled()) {
      throw new Error('native_push_disabled');
    }
    store.write(registration);
    // A repair after binding is its own request: never also a background rotation.
    const bindingRepair = registration.intent === 'binding_repair';
    const result = await postWebPushSubscription(
      api,
      {
        endpoint: registration.endpoint,
        keys: registration.keys,
      },
      await nativeDeviceLabel(),
      undefined,
      registration.previousEndpoints,
      bindingRepair ? undefined : recoverOnly,
      registration.intent,
    );
    if (intent && !intent.store.isCurrentIntentRevision(intent.revision)) {
      await nativePushUnsubscribe();
      store.clear();
      throw new Error('native_push_stale_intent');
    }
    if (!result.accepted) {
      if (bindingRepair) {
        // Final: it was turned off, or is not this device's to take back. Nothing
        // is left waiting, so no later post can turn it into a new registration.
        store.clear();
        throw new Error('binding_repair_not_accepted');
      }
      if (recoverOnly) {
        await nativePushUnsubscribe();
        store.clear();
        throw new Error('recovery_not_authorized');
      }
      throw new Error('native_push_registration_not_accepted');
    }
    if (intent) {
      assertNativePushIntentCurrent(intent);
    } else if (disabledIntentStore?.isDisabled()) {
      throw new Error('native_push_disabled');
    }
    store.clear();
    await rememberWebPushEndpoint(registration.endpoint);
  });
}

async function enableNativeWebPush(
  api: ApiContextType,
  publicKey: string,
  existing: Awaited<ReturnType<typeof nativePushStatus>>,
  options: EnableWebPushOptions,
  respectDisabledIntent: boolean,
  intent?: NativePushIntentContext,
  subscribeIntent?: WebPushSubscribeIntent,
): Promise<PushSubscriptionJSON> {
  const disabledIntentStore =
    options.disabledIntentStore ?? getBrowserNativePushDisabledIntentStore();
  if (intent) {
    assertNativePushIntentCurrent(intent);
  } else if (respectDisabledIntent && disabledIntentStore.isDisabled()) {
    throw new Error('native_push_disabled');
  }
  const subscription = await withNativePushLifecycleLock(async () => {
    if (intent) {
      assertNativePushIntentCurrent(intent);
    } else if (respectDisabledIntent && disabledIntentStore.isDisabled()) {
      throw new Error('native_push_disabled');
    }
    return existing?.state === 'on' && !options.forceResubscribe
      ? existing
      : await nativePushSubscribe(publicKey, Boolean(options.forceResubscribe));
  });
  if (!subscription) throw new Error('native_push_unavailable');
  if (intent && !intent.store.isCurrentIntentRevision(intent.revision)) {
    await withNativePushLifecycleLock(() => nativePushUnsubscribe());
    throw new Error('native_push_stale_intent');
  }
  if (!intent && respectDisabledIntent && disabledIntentStore.isDisabled()) {
    await withNativePushLifecycleLock(() => nativePushUnsubscribe());
    throw new Error('native_push_disabled');
  }
  const json: PushSubscriptionJSON = {
    endpoint: subscription.endpoint,
    keys: subscription.keys,
  };
  const previousEndpoints = [
    ...(existing?.state === 'on' ? [existing.endpoint] : []),
    ...await getRememberedWebPushEndpoints(),
  ].filter((endpoint, index, endpoints) => endpoints.indexOf(endpoint) === index);
  await postNativePushRegistration(
    api,
    {
      endpoint: subscription.endpoint,
      keys: subscription.keys,
      previousEndpoints,
      vapidPublicKey: publicKey,
      ...(subscribeIntent ? { intent: subscribeIntent } : {}),
    },
    options.pendingRegistrationStore ?? browserNativePushPendingRegistrationStore,
    options.recoverOnly,
    respectDisabledIntent ? disabledIntentStore : undefined,
    intent,
  );
  if (intent) assertNativePushIntentCurrent(intent);
  return json;
}

async function enableWebPushOnce(
  api: ApiContextType,
  options: EnableWebPushOptions = {},
  explicitEnable = false,
  existingIntent?: NativePushIntentContext,
): Promise<PushSubscriptionJSON> {
  if (hasNativeCapability('push')) {
    const disabledIntentStore =
      options.disabledIntentStore ?? getBrowserNativePushDisabledIntentStore();
    const intent: NativePushIntentContext = existingIntent ?? await withNativePushLifecycleLock(async () => {
      const intentState = disabledIntentStore.readIntent();
      const transition = explicitEnable
        ? disabledIntentStore.beginEnable()
        : intentState;
      if (!explicitEnable && intentState.userIntent === 'disabled') {
        throw new Error('native_push_disabled');
      }
      return {
        store: disabledIntentStore,
        revision: transition.revision,
      };
    });
    assertNativePushIntentCurrent(intent);
    const publicKey = (await api.getWebPushVapidPublicKey()).public_key;
    assertNativePushIntentCurrent(intent);
    const existing = await nativePushStatus(publicKey);
    return enableNativeWebPush(
      api,
      publicKey,
      existing,
      {
        ...options,
        disabledIntentStore,
      },
      !explicitEnable,
      intent,
      // `enableWebPush` is the press, and it alone hands an intent in.
      (explicitEnable || existingIntent !== undefined) && !options.recoverOnly
        ? 'explicit_enable'
        : undefined,
    );
  }
  const support = getWebPushSupportState();
  if (!support.supported) {
    throw new Error(support.reason);
  }
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw new Error('permission_denied');
  }

  const registration = await navigator.serviceWorker.register('/push-sw.js');
  const serverKey = urlBase64ToArrayBuffer((await api.getWebPushVapidPublicKey()).public_key);
  const existing = await registration.pushManager.getSubscription();
  let current = existing;
  if (
    existing
    && (options.forceResubscribe
      || !arrayBuffersEqual(existing.options.applicationServerKey, serverKey))
  ) {
    await existing.unsubscribe();
    current = await registration.pushManager.getSubscription();
    if (current?.endpoint === existing.endpoint) {
      throw new Error('unsubscribe_failed');
    }
  }
  const subscription =
    current ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: serverKey,
    }));

  const json = subscription.toJSON();
  const endpoint = typeof json.endpoint === 'string' ? json.endpoint : undefined;
  const previousEndpoints = [
    ...(existing?.endpoint ? [existing.endpoint] : []),
    ...await getRememberedWebPushEndpoints(),
  ];
  const pressed = explicitEnable && !options.recoverOnly;
  const result = await postWebPushSubscription(
    api,
    json,
    undefined,
    await getWebPushDeviceId(),
    previousEndpoints,
    options.recoverOnly,
    pressed ? 'explicit_enable' : undefined,
  );
  if (options.recoverOnly && !result.accepted) {
    await subscription.unsubscribe();
    throw new Error('recovery_not_authorized');
  }
  // On a bound Avibe the instance can refuse even a press (the endpoint is a
  // signed-in account's): the switch must not then read as on.
  if (pressed && result.accepted === false) {
    throw new Error('registration_not_accepted');
  }
  await rememberWebPushEndpoint(endpoint);
  return json;
}

/** The page is open at a loopback address: in a browser, only on the computer that runs Avibe. */
function isLoopbackPage(): boolean {
  const host = window.location?.hostname ?? '';
  return host === 'localhost' || host === '[::1]' || /^127(?:\.\d{1,3}){3}$/.test(host);
}

/** What to ask the instance about: this browser's live subscription, else the last endpoint it remembers. */
async function browserPushStatusPayload(
  existing: PushSubscription | null,
  rememberedEndpoints: string[],
): Promise<WebPushStatusPayload | undefined> {
  if (existing) {
    return {
      endpoint: existing.endpoint,
      subscription: existing.toJSON(),
      device_id: await getWebPushDeviceId(),
      previous_endpoints: rememberedEndpoints,
    };
  }
  return rememberedEndpoints[0] ? { endpoint: rememberedEndpoints[0] } : undefined;
}

/**
 * Whether binding this Avibe to an account stopped a subscription this browser
 * may take back. Either the instance says so of the endpoint it was asked about
 * (`current_subscription_binding_retired`), or the browser holds a subscription
 * the instance does not know and one of the endpoints it remembers was stopped:
 * the push service replaced the subscription meanwhile, or an earlier repair
 * subscribed again and never heard the answer. The remembered endpoints are
 * asked about only on the computer that runs Avibe, with notifications allowed.
 */
async function bindingRepairOffered(
  api: ApiContextType,
  status: WebPushStatus,
  existing: PushSubscription | null,
  rememberedEndpoints: string[],
): Promise<boolean> {
  if (status.current_subscription_binding_retired) return true;
  if (
    !existing
    || status.current_subscription_enabled
    || status.current_subscription_repairable
    || !isLoopbackPage()
    || typeof Notification === 'undefined'
    || Notification.permission !== 'granted'
  ) {
    return false;
  }
  for (const endpoint of rememberedEndpoints) {
    if (endpoint === existing.endpoint) continue;
    const earlier = await api.getWebPushStatus({ endpoint }).catch(() => null);
    if (earlier?.current_subscription_binding_retired) return true;
  }
  return false;
}

/**
 * Takes back, in this browser, the subscription that binding stopped, once
 * `bindingRepairOffered` says it may. Only while the browser already allows
 * notifications: it never asks. The live subscription is posted as it is, with
 * the other endpoints the browser remembers; a browser that lost its own
 * subscribes again first. A refusal is final (it was turned off meanwhile, or
 * is not this browser's): no other post follows, and a subscription made only
 * for the attempt is removed again. When no answer arrives the instance may
 * have recorded it: the subscription is kept, and the next foreground asks
 * about it. True when the instance accepted it.
 */
async function restoreBrowserPushAfterBinding(
  api: ApiContextType,
  existing: PushSubscription | null,
  publicKey: string,
  rememberedEndpoints: string[],
): Promise<boolean> {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return false;
  const deviceId = await getWebPushDeviceId();
  const subscription = existing ?? await (await navigator.serviceWorker.register('/push-sw.js'))
    .pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToArrayBuffer(publicKey),
    });
  const json = subscription.toJSON();
  const earlier = rememberedEndpoints.filter((endpoint) => endpoint !== subscription.endpoint);
  let outcome: 'accepted' | 'refused' | 'unknown';
  try {
    const result = await api.subscribeWebPush(
      json,
      undefined,
      deviceId,
      earlier.length ? earlier : undefined,
      undefined,
      'binding_repair',
    );
    outcome = result.accepted === true ? 'accepted' : 'refused';
  } catch (error) {
    // A 4xx is the instance answering that it recorded nothing. Anything else
    // (no connection, a reply cut short, a 5xx) leaves that open.
    const status = (error as { status?: unknown } | null)?.status;
    outcome = typeof status === 'number' && status >= 400 && status < 500 ? 'refused' : 'unknown';
  }
  if (outcome === 'accepted') await rememberWebPushEndpoint(json.endpoint ?? undefined);
  if (outcome === 'refused' && !existing) await subscription.unsubscribe().catch(() => false);
  return outcome === 'accepted';
}

let webPushLifecycleOperation: Promise<unknown> = Promise.resolve();

function enqueueWebPushLifecycleOperation<T>(operation: () => Promise<T>): Promise<T> {
  const next = webPushLifecycleOperation.then(operation, operation);
  webPushLifecycleOperation = next.then(
    () => undefined,
    () => undefined,
  );
  return next;
}

export async function enableWebPush(
  api: ApiContextType,
  options: EnableWebPushOptions = {},
): Promise<PushSubscriptionJSON> {
  if (hasNativeCapability('push')) {
    const disabledIntentStore =
      options.disabledIntentStore ?? getBrowserNativePushDisabledIntentStore();
    const intentPromise = withNativePushLifecycleLock(async () => {
      const transition = disabledIntentStore.beginEnable();
      return {
        store: disabledIntentStore,
        revision: transition.revision,
      };
    });
    return intentPromise.then((intent) => enqueueWebPushLifecycleOperation(
      () => enableWebPushOnce(
        api,
        {
          ...options,
          disabledIntentStore,
        },
        false,
        intent,
      ),
    ));
  }
  return enqueueWebPushLifecycleOperation(
    () => enableWebPushOnce(api, options, true),
  );
}

export type WebPushDisableResult = {
  accepted: true;
  status: 'disabled' | 'pending' | 'failed';
  remoteRevoked: boolean;
  instanceCleanup: NativePushInstanceCleanupState;
  nativeRevoked: NativePushRevokeCleanupState;
};

function nativePushCleanupStatus(
  record: NativePushDisableRecord,
): WebPushDisableResult['status'] {
  if (
    record.instanceCleanup === 'confirmed'
    && (record.nativeRevoke === 'confirmed' || record.nativeRevoke === 'not_needed')
  ) {
    return 'disabled';
  }
  if (
    record.instanceCleanup === 'failed'
    || record.instanceCleanup === 'unknown'
    || record.nativeRevoke === 'failed'
    || record.nativeRevoke === 'unknown'
  ) {
    return 'failed';
  }
  return 'pending';
}

function nativePushDisableResult(
  record: NativePushDisableRecord,
): WebPushDisableResult {
  return {
    accepted: true,
    status: nativePushCleanupStatus(record),
    remoteRevoked: record.instanceCleanup === 'confirmed',
    instanceCleanup: record.instanceCleanup,
    nativeRevoked: record.nativeRevoke,
  };
}

function persistNativePushCleanupPatch(
  store: NativePushDisabledIntentStore,
  patch: Parameters<NativePushDisabledIntentStore['update']>[0],
): NativePushDisableRecord | null {
  try {
    return store.update(patch);
  } catch {
    return null;
  }
}

async function disableNativeWebPushOnce(
  api: ApiContextType,
  intentStore: NativePushDisabledIntentStore,
  intent: NativePushIntentContext,
): Promise<WebPushDisableResult> {
  let record = intentStore.read();
  if (!record) {
    throw new Error('native_push_disable_persistence_failed');
  }
  assertNativePushIntentCurrent(intent);
  const updateCleanup = (
    patch: Parameters<NativePushDisabledIntentStore['update']>[0],
    rememberEndpoint = false,
  ): Promise<NativePushDisableRecord | null> => (
    withNativePushLifecycleLock(async () => {
      assertNativePushIntentCurrent(intent);
      const updated = persistNativePushCleanupPatch(intentStore, patch);
      if (updated && rememberEndpoint) {
        await rememberWebPushEndpoint(updated.endpoint ?? undefined);
      }
      return updated;
    })
  );

  if (record.phase === 'capture_pending') {
    try {
      const publicKey = (await api.getWebPushVapidPublicKey()).public_key;
      assertNativePushIntentCurrent(intent);
      const statusOutcome = await nativePushStatusOutcome(publicKey);
      if (statusOutcome.status === 'failed') {
        record = await updateCleanup({
          phase: 'capture_pending',
          nativeRevoke: 'failed',
        }) ?? record;
        return nativePushDisableResult(record);
      }
      const nativeStatus = statusOutcome.value;
      const revokeState = nativeStatus.revokeState
        ?? (nativeStatus.state === 'on' ? 'active' : null);
      if (revokeState === 'absent') {
        record = await updateCleanup({
          phase: 'cleanup',
          instanceCleanup: 'confirmed',
          nativeRevoke: 'not_needed',
        }) ?? record;
        return nativePushDisableResult(record);
      }
      if (
        (revokeState === 'active' || revokeState === 'pending')
        && typeof nativeStatus.endpoint === 'string'
        && nativeStatus.endpoint.length > 0
      ) {
        const captured = await updateCleanup({
          phase: 'cleanup',
          endpoint: nativeStatus.endpoint,
          instanceCleanup: 'pending',
          nativeRevoke: 'pending',
        }, true);
        if (!captured) return nativePushDisableResult(record);
        record = captured;
      } else {
        record = await updateCleanup({
          phase: 'capture_pending',
          nativeRevoke: revokeState === 'pending' ? 'pending' : 'failed',
        }) ?? record;
        return nativePushDisableResult(record);
      }
    } catch {
      record = await updateCleanup({
        phase: 'capture_pending',
        nativeRevoke: 'failed',
      }) ?? record;
      return nativePushDisableResult(record);
    }
  }

  if (!record.endpoint) {
    return nativePushDisableResult(record);
  }
  const endpoint = record.endpoint;

  if (record.instanceCleanup !== 'confirmed') {
    record = await withNativePushLifecycleLock(async () => {
      assertNativePushIntentCurrent(intent);
      try {
        const result = await api.unsubscribeWebPush(endpoint);
        return persistNativePushCleanupPatch(intentStore, {
          instanceCleanup: result.disabled === true ? 'confirmed' : 'failed',
        }) ?? record;
      } catch {
        return persistNativePushCleanupPatch(intentStore, {
          instanceCleanup: 'failed',
        }) ?? record;
      }
    }) ?? record;
  }

  if (record.nativeRevoke !== 'confirmed' && record.nativeRevoke !== 'not_needed') {
    record = await withNativePushLifecycleLock(async () => {
      assertNativePushIntentCurrent(intent);
      const nativeOutcome = await nativePushUnsubscribeOutcome();
      return persistNativePushCleanupPatch(intentStore, {
        nativeRevoke: nativeOutcome.status,
      }) ?? record;
    }) ?? record;
  }

  return nativePushDisableResult(record);
}

async function disableWebPushOnce(
  api: ApiContextType,
  nativeDisable?: {
    store: NativePushDisabledIntentStore;
    intent: NativePushIntentContext;
  },
): Promise<WebPushDisableResult> {
  if (nativeDisable) {
    return disableNativeWebPushOnce(api, nativeDisable.store, nativeDisable.intent);
  }
  const subscription = await getExistingWebPushSubscription();
  const endpoint = subscription?.endpoint;
  let remoteRevoked = !endpoint;
  if (endpoint) {
    const result = await api.unsubscribeWebPush(endpoint, await getWebPushDeviceId());
    remoteRevoked = result.disabled === true;
  }
  let browserRevoked = true;
  if (subscription) {
    browserRevoked = await subscription.unsubscribe();
  }
  return {
    accepted: true,
    status: remoteRevoked && browserRevoked ? 'disabled' : 'failed',
    remoteRevoked,
    instanceCleanup: remoteRevoked ? 'confirmed' : 'failed',
    nativeRevoked: 'not_needed',
  };
}

export async function disableWebPush(
  api: ApiContextType,
  disabledIntentStore?: NativePushDisabledIntentStore,
): Promise<WebPushDisableResult> {
  if (hasNativeCapability('push')) {
    const store = disabledIntentStore ?? getBrowserNativePushDisabledIntentStore();
    const intentPromise = withNativePushLifecycleLock(async () => {
      const transition = store.beginDisable();
      return {
        store,
        revision: transition.revision,
      };
    });
    return intentPromise.then((intent) => enqueueWebPushLifecycleOperation(
      () => disableWebPushOnce(api, {
        store,
        intent,
      }),
    ));
  }
  return enqueueWebPushLifecycleOperation(
    () => disableWebPushOnce(api),
  );
}

let webPushReconciliation: Promise<WebPushReconcileResult> | null = null;

async function reconcileWebPushOnce(
  api: ApiContextType,
  pendingRegistrationStore: NativePushPendingRegistrationStore,
  disabledIntentStore?: NativePushDisabledIntentStore,
): Promise<WebPushReconcileResult> {
  const support = getWebPushSupportState();
  if (hasNativeCapability('push')) {
    const intentStore = disabledIntentStore ?? getBrowserNativePushDisabledIntentStore();
    try {
      const intentState = intentStore.readIntent();
      const intent: NativePushIntentContext = {
        store: intentStore,
        revision: intentState.revision,
      };
      const runIntentOperation = async <T>(
        operation: () => Promise<T>,
      ): Promise<T> => {
        assertNativePushIntentCurrent(intent);
        return operation();
      };
      const disabledRecord = intentStore.read();
      if (disabledRecord) {
        const disabledStatus = nativePushCleanupStatus(disabledRecord);
        return {
          support,
          status: disabledStatus === 'disabled'
            ? 'disabled'
            : disabledStatus === 'pending'
              ? 'disable_pending'
              : 'disable_failed',
        };
      }
      assertNativePushIntentCurrent(intent);
      const publicKey = (await api.getWebPushVapidPublicKey()).public_key;
      assertNativePushIntentCurrent(intent);
      const nativeState = await nativePushStatus(publicKey);
      if (!nativeState || nativeState.state === 'unavailable') {
        return { support, status: 'disabled' };
      }
      if (nativeState.state !== 'on') {
        // No choice of "off" is stored for this instance (a sign-in starts
        // from a fresh account store) and iOS already allows Avibe's
        // notifications: turn them on, as the user had them. Never when
        // permission is not yet asked, so the system prompt never appears
        // on its own.
        const restoresByDefault = nativeState.permission === 'granted'
          && intentState.userIntent !== 'disabled';
        if (!restoresByDefault) {
          // Otherwise only a remembered subscription the instance still
          // knows is turned back on.
          const rememberedEndpoint = (await getRememberedWebPushEndpoints())[0];
          if (!rememberedEndpoint) {
            return { support, status: 'disabled' };
          }
          const rememberedStatus = await api.getWebPushStatus({
            endpoint: rememberedEndpoint,
          });
          if (
            !rememberedStatus.current_subscription_enabled
            && !rememberedStatus.current_subscription_repairable
          ) {
            return { support, status: 'disabled' };
          }
        }
        await runIntentOperation(() => enableNativeWebPush(
          api,
          publicKey,
          nativeState,
          {
            pendingRegistrationStore,
            disabledIntentStore: intentStore,
          },
          true,
          intent,
        ));
        return { support, status: 'enabled' };
      }
      const previousEndpoints = [
        nativeState.endpoint,
        ...await getRememberedWebPushEndpoints(),
      ].filter((endpoint, index, endpoints) => endpoints.indexOf(endpoint) === index);
      const nativeSubscription = {
        endpoint: nativeState.endpoint,
        keys: nativeState.keys,
      };
      const pendingRegistration = pendingRegistrationStore.read();
      if (
        pendingRegistration
        && pendingRegistration.endpoint === nativeState.endpoint
        && pendingRegistration.vapidPublicKey === publicKey
      ) {
        await runIntentOperation(() => postNativePushRegistration(
          api,
          pendingRegistration,
          pendingRegistrationStore,
          undefined,
          intentStore,
          intent,
        ));
        return { support, status: 'enabled' };
      }
      const serverStatus = await api.getWebPushStatus({
        endpoint: nativeState.endpoint,
        subscription: nativeSubscription,
        device_label: await nativeDeviceLabel(),
        previous_endpoints: previousEndpoints,
      });
      if (serverStatus.current_subscription_enabled) {
        await withNativePushLifecycleLock(async () => {
          assertNativePushIntentCurrent(intent);
          if (pendingRegistration) pendingRegistrationStore.clear();
          await rememberWebPushEndpoint(nativeState.endpoint);
        });
        return { support, status: 'enabled' };
      }
      if (serverStatus.current_subscription_binding_retired) {
        // Binding stopped it and this page is on the computer that runs Avibe:
        // take the same subscription back, only while notifications are still
        // allowed. A refusal throws and reads as off.
        if (nativeState.permission !== 'granted') {
          return { support, status: 'disabled' };
        }
        await runIntentOperation(() => postNativePushRegistration(
          api,
          {
            endpoint: nativeState.endpoint,
            keys: nativeState.keys,
            previousEndpoints: [],
            vapidPublicKey: publicKey,
            intent: 'binding_repair',
          },
          pendingRegistrationStore,
          undefined,
          intentStore,
          intent,
        ));
        return { support, status: 'enabled' };
      }
      if (serverStatus.current_subscription_repairable) {
        try {
          await runIntentOperation(() => enableNativeWebPush(
            api,
            publicKey,
            nativeState,
            {
              forceResubscribe: true,
              recoverOnly: true,
              pendingRegistrationStore,
              disabledIntentStore: intentStore,
            },
            true,
            intent,
          ));
          const repaired = await nativePushStatus(publicKey);
          if (repaired?.state === 'on') {
            const repairedStatus = await api.getWebPushStatus({
              endpoint: repaired.endpoint,
              subscription: {
                endpoint: repaired.endpoint,
                keys: repaired.keys,
              },
              device_label: await nativeDeviceLabel(),
              previous_endpoints: previousEndpoints,
            });
            if (repairedStatus.current_subscription_enabled) {
              await withNativePushLifecycleLock(async () => {
                assertNativePushIntentCurrent(intent);
                await rememberWebPushEndpoint(repaired.endpoint);
              });
              return { support, status: 'enabled' };
            }
          }
        } catch {
          // Keep the next foreground or manual settings action recoverable.
        }
      } else {
        await runIntentOperation(() => postNativePushRegistration(
          api,
          {
            endpoint: nativeState.endpoint,
            keys: nativeState.keys,
            previousEndpoints,
            vapidPublicKey: publicKey,
          },
          pendingRegistrationStore,
          undefined,
          intentStore,
          intent,
        ));
        return { support, status: 'enabled' };
      }
      return { support, status: 'disabled' };
    } catch (error) {
      return {
        support,
        status: nativeRegistrationAwaitsInstance(pendingRegistrationStore, error)
          ? 'registering'
          : 'disabled',
      };
    }
  }

  if (!support.supported) {
    return {
      support,
      status: support.reason === 'ios_requires_standalone' ? 'needs_install' : 'unsupported',
    };
  }

  try {
    const existing = await getExistingWebPushSubscription();
    const previousEndpoints = await getRememberedWebPushEndpoints();
    const rememberedEndpoint = previousEndpoints[0];
    const serverStatus = await api
      .getWebPushStatus(await browserPushStatusPayload(existing, previousEndpoints))
      .catch(() => null);
    if (serverStatus && await bindingRepairOffered(api, serverStatus, existing, previousEndpoints)) {
      const restored = await restoreBrowserPushAfterBinding(
        api,
        existing,
        serverStatus.public_key,
        previousEndpoints,
      ).catch(() => false);
      return { support, status: restored ? 'enabled' : 'disabled' };
    }
    const repairable = Boolean(serverStatus && (
      serverStatus.current_subscription_repairable
      || (!existing && Boolean(rememberedEndpoint) && serverStatus.current_subscription_enabled)
      || (existing && serverStatus.current_subscription_enabled
        && !webPushSubscriptionUsesVapidKey(existing, serverStatus.public_key))
    ));
    if (
      repairable
      && typeof Notification !== 'undefined'
      && Notification.permission === 'granted'
    ) {
      try {
        await enableWebPushOnce(api, { forceResubscribe: Boolean(existing), recoverOnly: true });
        const repaired = await getExistingWebPushSubscription();
        if (repaired) {
          const repairedStatus = await api.getWebPushStatus({
            endpoint: repaired.endpoint,
            subscription: repaired.toJSON(),
            device_id: await getWebPushDeviceId(),
            previous_endpoints: await getRememberedWebPushEndpoints(),
          });
          if (repairedStatus.current_subscription_enabled) {
            await rememberWebPushEndpoint(repaired.endpoint);
            return { support, status: 'enabled' };
          }
        }
      } catch {
        // Keep the control recoverable; the user can still retry manually.
      }
      return { support, status: 'disabled' };
    }
    if (existing && serverStatus?.current_subscription_enabled) {
      await rememberWebPushEndpoint(existing.endpoint);
    }
    return {
      support,
      status: existing && serverStatus?.current_subscription_enabled ? 'enabled' : 'disabled',
    };
  } catch {
    return { support, status: 'disabled' };
  }
}

/** Refusals: no later attempt with the same subscription and intent can succeed. */
const NATIVE_PUSH_REFUSALS = new Set([
  'binding_repair_not_accepted',
  'native_push_disabled',
  'native_push_stale_intent',
  'native_push_registration_not_accepted',
  'recovery_not_authorized',
]);

/**
 * Whether a failed native reconcile left a subscription waiting for the instance
 * that a retry can still register: the pending registration is written before
 * its POST and cleared once the instance accepts it (or refuses it for good), so
 * it is there exactly while the instance has yet to store this device. A refusal,
 * or a 4xx other than a timeout or rate limit, will not pass on its own.
 */
function nativeRegistrationAwaitsInstance(
  store: NativePushPendingRegistrationStore,
  error: unknown,
): boolean {
  if (error instanceof Error && NATIVE_PUSH_REFUSALS.has(error.message)) return false;
  const status = (error as { status?: unknown } | null)?.status;
  if (
    typeof status === 'number'
    && status >= 400
    && status < 500
    && status !== 408
    && status !== 429
  ) {
    return false;
  }
  try {
    return store.read() !== null;
  } catch {
    return false;
  }
}

/** Reconcile the current native or browser subscription with the instance once at a time. */
export function reconcileWebPush(
  api: ApiContextType,
  pendingRegistrationStore: NativePushPendingRegistrationStore =
    browserNativePushPendingRegistrationStore,
  disabledIntentStore?: NativePushDisabledIntentStore,
): Promise<WebPushReconcileResult> {
  if (!webPushReconciliation) {
    webPushReconciliation = enqueueWebPushLifecycleOperation(
      () => reconcileWebPushOnce(
        api,
        pendingRegistrationStore,
        disabledIntentStore,
      ),
    ).finally(() => {
      webPushReconciliation = null;
    });
  }
  return webPushReconciliation;
}

let bindingRestore: Promise<boolean> | null = null;

/**
 * In a browser on the computer that runs Avibe: when binding this Avibe to an
 * account stopped this browser's subscription, take it back. Called when the
 * page opens or comes back to the foreground, never from a worker. It sends
 * nothing unless the page is at a loopback address and the browser already
 * allows notifications, and it never asks for permission. Turning notifications
 * off is honoured: this runs in turn with enabling and disabling, and the
 * instance refuses a subscription that was turned off. True when this
 * browser's subscription is enabled again.
 */
export function restoreWebPushAfterBinding(api: ApiContextType): Promise<boolean> {
  if (
    hasNativeCapability('push')
    || !getWebPushSupportState().supported
    || !isLoopbackPage()
    || typeof Notification === 'undefined'
    || Notification.permission !== 'granted'
  ) {
    return Promise.resolve(false);
  }
  bindingRestore ??= enqueueWebPushLifecycleOperation(async () => {
    try {
      const existing = await getExistingWebPushSubscription();
      const rememberedEndpoints = await getRememberedWebPushEndpoints();
      const payload = await browserPushStatusPayload(existing, rememberedEndpoints);
      if (!payload) return false;
      const status = await api.getWebPushStatus(payload);
      if (existing && status.current_subscription_enabled) {
        // Already on, as after a repair whose answer never arrived: only remember it.
        if (rememberedEndpoints[0] !== existing.endpoint) await rememberWebPushEndpoint(existing.endpoint);
        return true;
      }
      if (!(await bindingRepairOffered(api, status, existing, rememberedEndpoints))) return false;
      return await restoreBrowserPushAfterBinding(api, existing, status.public_key, rememberedEndpoints);
    } catch {
      // The next time the page comes to the foreground tries again.
      return false;
    }
  }).finally(() => {
    bindingRestore = null;
  });
  return bindingRestore;
}

/**
 * Whether this device gets push notifications: `on` (this device's subscription is registered
 * and enabled), `off` (push could be turned on here), or `unavailable` (the browser cannot, iOS
 * needs the app on the Home Screen first, push is not set up on the server, or the read failed).
 * It changes nothing, with one exception: a subscription that binding stopped is first taken back
 * (`restoreWebPushAfterBinding`), so that it is not reported as off for the moment that takes.
 * Turning it on, or repairing a subscription, is `enableWebPush`'s job.
 */
export async function readWebPushDeviceState(api: ApiContextType): Promise<'on' | 'off' | 'unavailable'> {
  if (hasNativeCapability('push')) {
    try {
      if (getBrowserNativePushDisabledIntentStore().isDisabled()) return 'off';
      const publicKey = (await api.getWebPushVapidPublicKey()).public_key;
      const status = await nativePushStatus(publicKey);
      return status?.state === 'on' ? 'on' : 'off';
    } catch {
      return 'unavailable';
    }
  }
  if (!getWebPushSupportState().supported) return 'unavailable';
  try {
    const existing = await getExistingWebPushSubscription();
    const status = await api.getWebPushStatus(
      await browserPushStatusPayload(existing, await getRememberedWebPushEndpoints()),
    );
    if (!status?.ok || !status.configured) return 'unavailable';
    if (existing && status.current_subscription_enabled) return 'on';
    return (await restoreWebPushAfterBinding(api)) ? 'on' : 'off';
  } catch {
    return 'unavailable';
  }
}
