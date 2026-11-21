// What a page does when the address it is open on stops being this Avibe's
// address (docs/plans/instance-address-change.md, rules D1-D4).
//
// The old name is kept for a waiting period and then passes to someone else,
// who can serve a page there and read whatever this browser still holds under
// it. So the page empties everything under its own origin before it leaves.
// Everything script can reach is removed without any request. The HTTP cache
// needs one answer from the old address; the page goes to the new address only
// when that arrived too, otherwise to the old address's root, where the
// instance answers the navigation with a response that clears.
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
/** Every counter by which another tab of this origin knows that what it
 *  captured is void: ``signOutGeneration.ts``, the local store's epoch, the
 *  boot session's generation. Clearing localStorage resets them all. */
const WRITE_FENCE_KEYS = [
  SIGN_OUT_GENERATION_KEY,
  'avibe.local-store.epoch.v1',
  'avibe.boot-session.generation.v1',
];
/** Left behind for the other tabs of this origin. The counters only stop the
 *  stores that have one; a draft, a layout, a recent file have none. So a tab
 *  that sees this marker asks the instance and, if the address is retired,
 *  clears itself and leaves too. */
const RETIRED_MARKER_KEY = 'avibe.address-retired.v1';
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
    // back.
    leaveWriteFence();
  });
  return complete;
}

function within<T>(promise: Promise<T>, ms: number, fallback: T): Promise<T> {
  return Promise.race([promise, new Promise<T>((resolve) => setTimeout(() => resolve(fallback), ms))]);
}

function leaveWriteFence(): void {
  // A value no tab can have captured: the time of this clearing. It is all that
  // stays behind under this address.
  const fence = String(Date.now());
  for (const key of [...WRITE_FENCE_KEYS, RETIRED_MARKER_KEY]) window.localStorage.setItem(key, fence);
}

/** One request to the old address, after the page emptied its storage. Script
 *  cannot reach the HTTP cache, where authenticated responses can sit for a
 *  year; the answer to this request clears it, and a browser honours that only
 *  on a request that carries credentials. The same request reports the receipt
 *  for the Owner's list. Resolves `true` when that answer arrived. */
async function finishAtRetiredAddress(): Promise<boolean> {
  try {
    const response = await within(
      fetch(CLEARED_REPORT_PATH, {
        method: 'POST',
        body: receipt ?? '',
        credentials: 'same-origin',
        cache: 'no-store',
        keepalive: true,
      }),
      REPORT_TIMEOUT_MS,
      null,
    );
    return response !== null && isRetiredAddressResponse(response);
  } catch {
    return false;
  }
}

/** Clear, report, leave. `movedTo` is the new origin when the stream's final
 *  frame named it. Runs once per page. */
export function leaveRetiredAddress(movedTo: string | null): Promise<void> {
  if (leaving) return leaving;
  leaving = (async () => {
    const emptied = await within(clearThisOrigin(), CLEAR_TIMEOUT_MS, false);
    const cleared = emptied && await finishAtRetiredAddress();
    // Only a page that emptied itself, HTTP cache included, goes on to the new
    // address. Otherwise it goes to the old address's root, where the response
    // to that navigation does the clearing.
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

let checkingMarker = false;

/** Another tab, or the cleaning page, left the marker. The marker alone proves
 *  nothing: a name that was taken back is this Avibe's address again and the
 *  marker is then stale. Only the instance's own answer decides. */
async function checkRetiredMarker(): Promise<void> {
  if (leaving || checkingMarker || typeof window === 'undefined') return;
  try {
    if (window.localStorage.getItem(RETIRED_MARKER_KEY) === null) return;
  } catch {
    return;
  }
  checkingMarker = true;
  try {
    const response = await fetch('/api/session', {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    if (isRetiredAddressResponse(response)) {
      noteRetiredAddressResponse();
    } else if (response.ok) {
      window.localStorage.removeItem(RETIRED_MARKER_KEY);
    }
  } catch {
    // Unreachable: this tab's next answered request decides (apiFetch).
  } finally {
    checkingMarker = false;
  }
}

if (typeof window !== 'undefined' && typeof document !== 'undefined') {
  window.addEventListener('storage', (event) => {
    if (event.key === RETIRED_MARKER_KEY && event.newValue !== null) void checkRetiredMarker();
  });
  // A tab that was frozen saw no storage event; it looks when it comes back.
  window.addEventListener('pageshow', () => void checkRetiredMarker());
  document.addEventListener('resume', () => void checkRetiredMarker());
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void checkRetiredMarker();
  });
  void checkRetiredMarker();
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
