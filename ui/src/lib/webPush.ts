import type { ApiContextType } from '@/context/ApiContext';
import { isIosDevice, isStandalonePwa } from './platform';

const WEB_PUSH_DEVICE_ID_KEY = 'vibe.webPush.deviceId';
const WEB_PUSH_ENDPOINTS_KEY = 'vibe.webPush.endpoints';
const WEB_PUSH_ENDPOINT_CACHE = 'avibe.web-push-endpoint.v1';
const WEB_PUSH_ENDPOINT_ENTRY_PATH = '/__avibe/web-push-endpoint';
const WEB_PUSH_DEVICE_ENTRY_PATH = '/__avibe/web-push-device-id';

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

export type EnableWebPushOptions = {
  forceResubscribe?: boolean;
  recoverOnly?: boolean;
};

export async function enableWebPush(
  api: ApiContextType,
  options: EnableWebPushOptions = {},
): Promise<PushSubscriptionJSON> {
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

export async function disableWebPush(api: ApiContextType): Promise<boolean> {
  const subscription = await getExistingWebPushSubscription();
  const endpoint = subscription?.endpoint;
  if (endpoint) {
    await api.unsubscribeWebPush(endpoint, await getWebPushDeviceId());
  }
  if (subscription) {
    await subscription.unsubscribe();
  }
  return Boolean(endpoint);
}
