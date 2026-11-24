// Taking back, in a browser on the computer that runs Avibe, the push subscription that binding
// the Avibe to an account stopped. Loaded on demand by `webPush.ts`, only once a repair can be on
// offer (`bindingRepairPossible`): the first screen never draws any of this.
import type { ApiContextType, WebPushStatus } from '@/context/ApiContext';
import {
  bindingRepairPossible,
  browserPushStatusPayload,
  getExistingWebPushSubscription,
  getRememberedWebPushEndpoints,
  getWebPushDeviceId,
  rememberWebPushEndpoint,
  urlBase64ToArrayBuffer,
} from './webPush';

/**
 * Whether binding this Avibe to an account stopped a subscription this browser
 * may take back. Either the instance says so of the endpoint it was asked about
 * (`current_subscription_binding_retired`), or the browser holds a subscription
 * the instance does not know and one of the endpoints it remembers was stopped:
 * the push service replaced the subscription meanwhile, or an earlier repair
 * subscribed again and never heard the answer. The remembered endpoints are
 * asked about only on the computer that runs Avibe, with notifications allowed.
 */
export async function bindingRepairOffered(
  api: ApiContextType,
  status: WebPushStatus,
  existing: PushSubscription | null,
  rememberedEndpoints: string[],
): Promise<boolean> {
  if (status.current_subscription_binding_retired) return true;
  if (!existing || !bindingRepairPossible(status, existing)) return false;
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
export async function restoreBrowserPushAfterBinding(
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

/**
 * One foreground attempt, as `restoreWebPushAfterBinding` runs it in turn with enabling and
 * disabling: ask the instance about this browser's subscription, and take it back when offered.
 * True when this browser's subscription is enabled afterwards.
 */
export async function restoreAfterBinding(api: ApiContextType): Promise<boolean> {
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
  return restoreBrowserPushAfterBinding(api, existing, status.public_key, rememberedEndpoints);
}
