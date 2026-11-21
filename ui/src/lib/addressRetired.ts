// What a page does when the address it is open on stops being this Avibe's
// address (docs/plans/instance-address-change.md, rules D1-D4).
//
// The old name is kept for a waiting period and then passes to someone else,
// who can serve a page there and read whatever this browser still holds under
// it. So the page empties everything under its own origin before it leaves.
// It needs no request to succeed for that; and it goes to the new address only
// when its own clearing finished, otherwise to the old address's root, where
// the instance answers with a response that clears.
import { blockBootSessionRecording } from './bootSession';
import { forgetLocalSnapshots } from './localSnapshots';
import { SIGN_OUT_GENERATION_KEY } from './signOutGeneration';

export const ADDRESS_RETIRED_EVENT = 'instance.address-retired';
const RETIRED_HEADER = 'X-Avibe-Address';
const RETIRED_HEADER_VALUE = 'retired';
const CLEARED_REPORT_PATH = '/__avibe/address-cleared';
const CLEAR_TIMEOUT_MS = 5_000;
const REPORT_TIMEOUT_MS = 2_000;
/** How long a response that says "retired" waits for the stream's own final
 *  frame, which alone names the new address. */
const FRAME_WAIT_MS = 3_000;
const LOCAL_DATABASES = ['avibe-local-store', 'avibe-local-snapshots'];
/** The two counters by which another tab of this origin knows that what it
 *  captured is void (``signOutGeneration.ts``, the local store's epoch). */
const WRITE_FENCE_KEYS = [SIGN_OUT_GENERATION_KEY, 'avibe.local-store.epoch.v1'];
const RECEIPT_SHAPE = /^[A-Za-z0-9_-]{43}$/;
/** The only shape a new address can have. The frame comes over this page's own
 *  authenticated stream; this is a second lock on where the page may be sent. */
const MANAGED_ORIGIN = /^https:\/\/[a-z0-9](?:[a-z0-9-]*[a-z0-9])?-app\.avibe\.bot$/;

let receipt: string | null = null;
let leaving: Promise<void> | null = null;
let streamIsOpen: () => boolean = () => false;

/** The value this session posts back once it has cleared. It arrives when the
 *  event stream connects and stays in memory only, so a tab that slept through
 *  the change can still report. */
export function rememberAddressReceipt(value: unknown): void {
  if (typeof value === 'string' && RECEIPT_SHAPE.test(value)) receipt = value;
}

export function setAddressStreamProbe(probe: () => boolean): void {
  streamIsOpen = probe;
}

export function isRetiredAddressResponse(response: { headers?: Pick<Headers, 'get'> | null }): boolean {
  return response.headers?.get(RETIRED_HEADER) === RETIRED_HEADER_VALUE;
}

function deleteDatabase(name: string): Promise<boolean> {
  return new Promise((resolve) => {
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.deleteDatabase(name);
    } catch {
      resolve(false);
      return;
    }
    request.onsuccess = () => resolve(true);
    request.onerror = () => resolve(false);
    // Another connection is still open and did not close when asked.
    request.onblocked = () => resolve(false);
  });
}

async function clearDatabases(): Promise<boolean> {
  if (typeof indexedDB === 'undefined') return true;
  const names = new Set(LOCAL_DATABASES);
  try {
    for (const entry of (await indexedDB.databases?.()) ?? []) {
      if (entry.name) names.add(entry.name);
    }
  } catch {
    // Listing is optional; the two stores this app owns are named above.
  }
  const results = await Promise.all([...names].map(deleteDatabase));
  return results.every(Boolean);
}

async function clearCaches(): Promise<void> {
  if (typeof caches === 'undefined') return;
  await Promise.all((await caches.keys()).map((key) => caches.delete(key)));
}

async function removeWorkers(): Promise<void> {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return;
  for (const registration of await navigator.serviceWorker.getRegistrations()) {
    try {
      await (await registration.pushManager?.getSubscription())?.unsubscribe();
    } catch {
      // A registration without push, or a browser that refuses: unregistering
      // below ends the subscription with it.
    }
    await registration.unregister();
  }
}

/** Delete everything this origin holds that script can reach. Resolves `true`
 *  only when every step succeeded. */
export async function clearThisOrigin(): Promise<boolean> {
  let complete = true;
  const step = async (run: () => Promise<boolean | void>) => {
    try {
      if ((await run()) === false) complete = false;
    } catch {
      complete = false;
    }
  };
  // First stop the stores from writing, in this tab and (through the shared
  // sign-out generation) in the others, so nothing is put back while it is
  // being removed.
  blockBootSessionRecording();
  await step(forgetLocalSnapshots);
  await step(clearDatabases);
  await step(clearCaches);
  await step(removeWorkers);
  await step(async () => {
    window.localStorage.clear();
    window.sessionStorage.clear();
    // Clearing also removed the counters that fence other tabs' writes, and a
    // tab that captured the starting value would pass again and put its data
    // back. Leave both at a value no tab can hold: the time of this clearing,
    // which is all that stays behind under this address.
    const fence = String(Date.now());
    for (const key of WRITE_FENCE_KEYS) window.localStorage.setItem(key, fence);
  });
  return complete;
}

function within<T>(promise: Promise<T>, ms: number, fallback: T): Promise<T> {
  return Promise.race([promise, new Promise<T>((resolve) => setTimeout(() => resolve(fallback), ms))]);
}

async function reportCleared(): Promise<void> {
  if (!receipt) return;
  try {
    await within(
      fetch(CLEARED_REPORT_PATH, {
        method: 'POST',
        body: receipt,
        credentials: 'omit',
        cache: 'no-store',
        keepalive: true,
      }).then(() => undefined),
      REPORT_TIMEOUT_MS,
      undefined,
    );
  } catch {
    // The report is for the Owner's list. Clearing never depends on it.
  }
}

/** Clear, report, leave. `movedTo` is the new origin when the stream's final
 *  frame named it. Runs once per page. */
export function leaveRetiredAddress(movedTo: string | null): Promise<void> {
  if (leaving) return leaving;
  leaving = (async () => {
    const cleared = await within(clearThisOrigin(), CLEAR_TIMEOUT_MS, false);
    if (cleared) await reportCleared();
    // Only a page that emptied itself goes on to the new address. Otherwise it
    // goes to the old address's root, where the response does the clearing.
    const target = cleared && movedTo && MANAGED_ORIGIN.test(movedTo)
      ? `${movedTo}${window.location.pathname}`
      : '/';
    window.location.replace(target);
  })();
  return leaving;
}

/** A response carried the retired marker. A page whose stream is still open
 *  gives the final frame a moment first, so a request that raced the frame does
 *  not cost it the new address. */
export function noteRetiredAddressResponse(): void {
  if (leaving || typeof window === 'undefined') return;
  if (!streamIsOpen()) {
    void leaveRetiredAddress(null);
    return;
  }
  window.setTimeout(() => {
    void leaveRetiredAddress(null);
  }, FRAME_WAIT_MS);
}

export function handleAddressRetiredFrame(data: string): void {
  let movedTo: string | null = null;
  try {
    const payload = JSON.parse(data) as { moved_to?: unknown; receipt?: unknown };
    rememberAddressReceipt(payload.receipt);
    if (typeof payload.moved_to === 'string') movedTo = payload.moved_to;
  } catch {
    // An unreadable frame still means this address is retired.
  }
  void leaveRetiredAddress(movedTo);
}
