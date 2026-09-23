import type { ApiContextType } from '@/context/ApiContext';
import { isIosDevice, isStandalonePwa } from './platform';

const WEB_PUSH_DEVICE_ID_KEY = 'vibe.webPush.deviceId';
const WEB_PUSH_ENDPOINTS_KEY = 'vibe.webPush.endpoints';
const WEB_PUSH_ENDPOINT_CACHE = 'avibe.web-push-endpoint.v1';
const WEB_PUSH_ENDPOINT_ENTRY_PATH = '/__avibe/web-push-endpoint';

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

export function getWebPushDeviceId(): string {
  try {
    const existing = window.localStorage.getItem(WEB_PUSH_DEVICE_ID_KEY);
    if (existing) return existing;
  } catch {
    // Storage can be blocked in hardened browsers/WebViews; keep notification
    // controls usable even if the id cannot persist across page loads.
  }
  const generated =
    window.crypto?.randomUUID?.() ??
    `device-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  try {
    window.localStorage.setItem(WEB_PUSH_DEVICE_ID_KEY, generated);
  } catch {
    // Best-effort persistence only.
  }
  return generated;
}

export function getRememberedWebPushEndpoints(): string[] {
  try {
    const raw = window.localStorage.getItem(WEB_PUSH_ENDPOINTS_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    if (Array.isArray(parsed)) {
      return parsed.filter((endpoint): endpoint is string => typeof endpoint === 'string' && endpoint.length > 0);
    }
  } catch {
    // Best-effort cleanup hints only.
  }
  return [];
}

export async function rememberWebPushEndpoint(endpoint: string | undefined): Promise<void> {
  if (!endpoint) return;
  const endpoints = [endpoint, ...getRememberedWebPushEndpoints().filter((candidate) => candidate !== endpoint)].slice(0, 8);
  try {
    window.localStorage.setItem(WEB_PUSH_ENDPOINTS_KEY, JSON.stringify(endpoints));
  } catch {
    // Best-effort persistence only.
  }
  if (!('caches' in window)) return;
  try {
    const cache = await window.caches.open(WEB_PUSH_ENDPOINT_CACHE);
    const entryUrl = new URL(WEB_PUSH_ENDPOINT_ENTRY_PATH, window.location.origin).href;
    await cache.put(entryUrl, new Response(JSON.stringify({ endpoint }), {
      headers: { 'content-type': 'application/json' },
    }));
  } catch {
    // Local endpoint history still supports foreground recovery.
  }
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

export type EnableWebPushOptions = {
  forceResubscribe?: boolean;
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
    ...getRememberedWebPushEndpoints(),
  ];
  await api.subscribeWebPush(json, undefined, getWebPushDeviceId(), previousEndpoints);
  await rememberWebPushEndpoint(endpoint);
  return json;
}

export async function disableWebPush(api: ApiContextType): Promise<boolean> {
  const subscription = await getExistingWebPushSubscription();
  const endpoint = subscription?.endpoint;
  if (subscription) {
    await subscription.unsubscribe();
  }
  if (endpoint) {
    await api.unsubscribeWebPush(endpoint, getWebPushDeviceId());
    return true;
  }
  return false;
}
