import type { ApiContextType } from '@/context/ApiContext';
import { isIosDevice, isStandalonePwa } from './platform';
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

export type WebPushReconcileStatus =
  | 'unsupported'
  | 'needs_install'
  | 'disabled'
  | 'disable_pending'
  | 'disable_failed'
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
export type NativePushDisableRecord = {
  version: 2;
  userIntent: 'disabled';
  endpoint: string | null;
  instanceCleanup: NativePushInstanceCleanupState;
  nativeRevoke: NativePushRevokeCleanupState;
};

export interface NativePushDisabledIntentStore {
  isDisabled(): boolean;
  read(): NativePushDisableRecord | null;
  beginDisable(): { record: NativePushDisableRecord; created: boolean };
  update(
    patch: Partial<Pick<
      NativePushDisableRecord,
      'endpoint' | 'instanceCleanup' | 'nativeRevoke'
    >>,
  ): NativePushDisableRecord;
  clear(): void;
}

export function createNativePushDisabledIntentStore(
  storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
): NativePushDisabledIntentStore {
  const failClosedRecord = (): NativePushDisableRecord => ({
    version: 2,
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
  const readRecord = (): NativePushDisableRecord | null => {
    const raw = storage.getItem(NATIVE_PUSH_DISABLED_INTENT_KEY);
    if (raw === null) return null;
    try {
      const parsed = JSON.parse(raw) as {
        version?: unknown;
        userIntent?: unknown;
        endpoint?: unknown;
        instanceCleanup?: unknown;
        nativeRevoke?: unknown;
        cleanup?: unknown;
      };
      const endpoint = typeof parsed.endpoint === 'string' && parsed.endpoint.length > 0
        ? parsed.endpoint
        : null;
      if (
        parsed.version === 2
        && parsed.userIntent === 'disabled'
        && isInstanceCleanupState(parsed.instanceCleanup)
        && isNativeRevokeState(parsed.nativeRevoke)
      ) {
        return {
          version: 2,
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
          ...failClosedRecord(),
          endpoint,
        };
      }
      return failClosedRecord();
    } catch {
      return failClosedRecord();
    }
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
    beginDisable: () => {
      const existing = readRecord();
      if (existing) return { record: existing, created: false };
      const record = write(failClosedRecord());
      return { record, created: true };
    },
    update: (patch) => {
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
      return write({
        ...existing,
        ...patch,
        endpoint: existing.endpoint ?? patch.endpoint ?? null,
        version: 2,
        userIntent: 'disabled',
      });
    },
    clear: () => {
      storage.removeItem(NATIVE_PUSH_DISABLED_INTENT_KEY);
      if (storage.getItem(NATIVE_PUSH_DISABLED_INTENT_KEY) !== null) {
        throw new Error('native_push_enable_persistence_failed');
      }
    },
  };
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

async function postNativePushRegistration(
  api: ApiContextType,
  registration: NativePushPendingRegistration,
  store: NativePushPendingRegistrationStore,
  recoverOnly?: boolean,
  disabledIntentStore?: NativePushDisabledIntentStore,
): Promise<void> {
  if (disabledIntentStore?.isDisabled()) {
    throw new Error('native_push_disabled');
  }
  store.write(registration);
  const result = await api.subscribeWebPush(
    {
      endpoint: registration.endpoint,
      keys: registration.keys,
    },
    'iPhone App',
    undefined,
    registration.previousEndpoints,
    recoverOnly,
  );
  if (!result.accepted) {
    if (recoverOnly) {
      await nativePushUnsubscribe();
      store.clear();
      throw new Error('recovery_not_authorized');
    }
    throw new Error('native_push_registration_not_accepted');
  }
  if (disabledIntentStore?.isDisabled()) {
    throw new Error('native_push_disabled');
  }
  store.clear();
  await rememberWebPushEndpoint(registration.endpoint);
}

async function enableNativeWebPush(
  api: ApiContextType,
  publicKey: string,
  existing: Awaited<ReturnType<typeof nativePushStatus>>,
  options: EnableWebPushOptions,
  respectDisabledIntent: boolean,
): Promise<PushSubscriptionJSON> {
  const disabledIntentStore =
    options.disabledIntentStore ?? getBrowserNativePushDisabledIntentStore();
  if (respectDisabledIntent && disabledIntentStore.isDisabled()) {
    throw new Error('native_push_disabled');
  }
  const subscription = existing?.state === 'on' && !options.forceResubscribe
    ? existing
    : await nativePushSubscribe(publicKey);
  if (!subscription) throw new Error('native_push_unavailable');
  if (respectDisabledIntent && disabledIntentStore.isDisabled()) {
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
    },
    options.pendingRegistrationStore ?? browserNativePushPendingRegistrationStore,
    options.recoverOnly,
    respectDisabledIntent ? disabledIntentStore : undefined,
  );
  return json;
}

async function enableWebPushOnce(
  api: ApiContextType,
  options: EnableWebPushOptions = {},
  explicitEnable = false,
): Promise<PushSubscriptionJSON> {
  if (hasNativeCapability('push')) {
    const disabledIntentStore =
      options.disabledIntentStore ?? getBrowserNativePushDisabledIntentStore();
    if (explicitEnable) {
      disabledIntentStore.clear();
    } else if (disabledIntentStore.isDisabled()) {
      throw new Error('native_push_disabled');
    }
    const publicKey = (await api.getWebPushVapidPublicKey()).public_key;
    const existing = await nativePushStatus(publicKey);
    return enableNativeWebPush(api, publicKey, existing, options, !explicitEnable);
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
  const result = await api.subscribeWebPush(
    json,
    undefined,
    await getWebPushDeviceId(),
    previousEndpoints,
    options.recoverOnly,
  );
  if (options.recoverOnly && !result.accepted) {
    await subscription.unsubscribe();
    throw new Error('recovery_not_authorized');
  }
  await rememberWebPushEndpoint(endpoint);
  return json;
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
  created: boolean,
): Promise<WebPushDisableResult> {
  let record = intentStore.read();
  if (!record) {
    throw new Error('native_push_disable_persistence_failed');
  }

  if (created) {
    try {
      const publicKey = (await api.getWebPushVapidPublicKey()).public_key;
      const statusOutcome = await nativePushStatusOutcome(publicKey);
      if (statusOutcome.status === 'failed') {
        record = persistNativePushCleanupPatch(intentStore, {
          nativeRevoke: 'failed',
        }) ?? record;
        return nativePushDisableResult(record);
      }
      const nativeStatus = statusOutcome.value;
      const revokeState = nativeStatus.revokeState
        ?? (nativeStatus.state === 'on' ? 'active' : null);
      if (revokeState === 'absent') {
        record = persistNativePushCleanupPatch(intentStore, {
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
        const captured = persistNativePushCleanupPatch(intentStore, {
          endpoint: nativeStatus.endpoint,
          instanceCleanup: 'pending',
          nativeRevoke: 'pending',
        });
        if (!captured) return nativePushDisableResult(record);
        record = captured;
        await rememberWebPushEndpoint(record.endpoint ?? undefined);
      } else {
        record = persistNativePushCleanupPatch(intentStore, {
          nativeRevoke: revokeState === 'pending' ? 'pending' : 'failed',
        }) ?? record;
        return nativePushDisableResult(record);
      }
    } catch {
      record = persistNativePushCleanupPatch(intentStore, {
        nativeRevoke: 'failed',
      }) ?? record;
      return nativePushDisableResult(record);
    }
  }

  if (!record.endpoint) {
    return nativePushDisableResult(record);
  }

  if (record.instanceCleanup !== 'confirmed') {
    try {
      const result = await api.unsubscribeWebPush(record.endpoint);
      record = persistNativePushCleanupPatch(intentStore, {
        instanceCleanup: result.disabled === true ? 'confirmed' : 'failed',
      }) ?? record;
    } catch {
      record = persistNativePushCleanupPatch(intentStore, {
        instanceCleanup: 'failed',
      }) ?? record;
    }
  }

  if (record.nativeRevoke !== 'confirmed' && record.nativeRevoke !== 'not_needed') {
    const nativeOutcome = await nativePushUnsubscribeOutcome();
    record = persistNativePushCleanupPatch(intentStore, {
      nativeRevoke: nativeOutcome.status,
    }) ?? record;
  }

  return nativePushDisableResult(record);
}

async function disableWebPushOnce(
  api: ApiContextType,
  nativeDisable?: {
    store: NativePushDisabledIntentStore;
    created: boolean;
  },
): Promise<WebPushDisableResult> {
  if (nativeDisable) {
    return disableNativeWebPushOnce(api, nativeDisable.store, nativeDisable.created);
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
  const nativeDisable = hasNativeCapability('push')
    ? {
        store: disabledIntentStore ?? getBrowserNativePushDisabledIntentStore(),
        created: false,
      }
    : undefined;
  if (nativeDisable) {
    nativeDisable.created = nativeDisable.store.beginDisable().created;
  }
  return enqueueWebPushLifecycleOperation(
    () => disableWebPushOnce(
      api,
      nativeDisable,
    ),
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
    try {
      const intentStore = disabledIntentStore ?? getBrowserNativePushDisabledIntentStore();
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
      const publicKey = (await api.getWebPushVapidPublicKey()).public_key;
      const nativeState = await nativePushStatus(publicKey);
      if (!nativeState || nativeState.state === 'unavailable') {
        return { support, status: 'disabled' };
      }
      if (nativeState.state !== 'on') {
        const rememberedEndpoints = await getRememberedWebPushEndpoints();
        const rememberedEndpoint = rememberedEndpoints[0];
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
        await enableNativeWebPush(api, publicKey, nativeState, {
          pendingRegistrationStore,
          disabledIntentStore: intentStore,
        }, true);
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
        await postNativePushRegistration(
          api,
          pendingRegistration,
          pendingRegistrationStore,
          undefined,
          intentStore,
        );
        return { support, status: 'enabled' };
      }
      const serverStatus = await api.getWebPushStatus({
        endpoint: nativeState.endpoint,
        subscription: nativeSubscription,
        device_label: 'iPhone App',
        previous_endpoints: previousEndpoints,
      });
      if (serverStatus.current_subscription_enabled) {
        if (pendingRegistration) pendingRegistrationStore.clear();
        await rememberWebPushEndpoint(nativeState.endpoint);
        return { support, status: 'enabled' };
      }
      if (serverStatus.current_subscription_repairable) {
        try {
          await enableNativeWebPush(api, publicKey, nativeState, {
            forceResubscribe: true,
            recoverOnly: true,
            pendingRegistrationStore,
            disabledIntentStore: intentStore,
          }, true);
          const repaired = await nativePushStatus(publicKey);
          if (repaired?.state === 'on') {
            const repairedStatus = await api.getWebPushStatus({
              endpoint: repaired.endpoint,
              subscription: {
                endpoint: repaired.endpoint,
                keys: repaired.keys,
              },
              device_label: 'iPhone App',
              previous_endpoints: previousEndpoints,
            });
            if (repairedStatus.current_subscription_enabled) {
              await rememberWebPushEndpoint(repaired.endpoint);
              return { support, status: 'enabled' };
            }
          }
        } catch {
          // Keep the next foreground or manual settings action recoverable.
        }
      } else {
        await postNativePushRegistration(
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
        );
        return { support, status: 'enabled' };
      }
      return { support, status: 'disabled' };
    } catch {
      return { support, status: 'disabled' };
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
      .getWebPushStatus(
        existing
          ? {
              endpoint: existing.endpoint,
              subscription: existing.toJSON(),
              device_id: await getWebPushDeviceId(),
              previous_endpoints: previousEndpoints,
            }
          : rememberedEndpoint ? { endpoint: rememberedEndpoint } : undefined,
      )
      .catch(() => null);
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

/**
 * Whether this device gets push notifications, read without changing anything: `on` (this
 * device's subscription is registered and enabled), `off` (push could be turned on here), or
 * `unavailable` (the browser cannot, iOS needs the app on the Home Screen first, push is not set
 * up on the server, or the read failed). Turning it on, or repairing a subscription, is
 * `enableWebPush`'s job.
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
    const status = await api.getWebPushStatus(existing
      ? {
          endpoint: existing.endpoint,
          subscription: existing.toJSON(),
          device_id: await getWebPushDeviceId(),
          previous_endpoints: await getRememberedWebPushEndpoints(),
        }
      : undefined);
    if (!status?.ok || !status.configured) return 'unavailable';
    return existing && status.current_subscription_enabled ? 'on' : 'off';
  } catch {
    return 'unavailable';
  }
}
