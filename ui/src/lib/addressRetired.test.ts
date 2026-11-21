// @vitest-environment jsdom
import 'fake-indexeddb/auto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Rules D2 and D3 of docs/plans/instance-address-change.md: a page on a
// retired address empties its own origin without needing any request, and goes
// to the new address only when that clearing finished.

const RECEIPT = 'r'.repeat(43);
const NEW_ORIGIN = 'https://new-app.avibe.bot';

function seedDatabase(name: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(name, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('rows');
    request.onsuccess = () => {
      const db = request.result;
      db.onversionchange = () => db.close();
      const tx = db.transaction('rows', 'readwrite');
      tx.objectStore('rows').put({ text: 'private' }, 'k');
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    };
    request.onerror = () => reject(request.error);
  });
}

async function databaseNames(): Promise<string[]> {
  return (await indexedDB.databases()).map((entry) => entry.name ?? '').sort();
}

async function load() {
  vi.resetModules();
  return import('./addressRetired');
}

let replaced: string[];
let unregistered: number;
let unsubscribed: number;
let cacheKeys: string[];
let fetchCalls: Array<{ url: string; init: RequestInit }>;

beforeEach(async () => {
  replaced = [];
  unregistered = 0;
  unsubscribed = 0;
  cacheKeys = ['avibe-web-push-endpoints', 'other'];
  fetchCalls = [];
  window.localStorage.setItem('avibe.session-drafts.v1', 'unsent text');
  window.localStorage.setItem('avibe.recent-files', '["secret.pdf"]');
  window.sessionStorage.setItem('scroll', '10');
  await seedDatabase('avibe-local-store');
  await seedDatabase('avibe-local-snapshots');
  await seedDatabase('something-else');
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { pathname: '/sessions/abc', search: '?token=x', hash: '#y', replace: (to: string) => replaced.push(to) },
  });
  vi.stubGlobal('caches', {
    keys: async () => [...cacheKeys],
    delete: async (key: string) => {
      cacheKeys = cacheKeys.filter((entry) => entry !== key);
      return true;
    },
  });
  Object.defineProperty(navigator, 'serviceWorker', {
    configurable: true,
    value: {
      getRegistrations: async () => [{
        pushManager: { getSubscription: async () => ({ unsubscribe: async () => { unsubscribed += 1; return true; } }) },
        unregister: async () => { unregistered += 1; return true; },
      }],
    },
  });
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    fetchCalls.push({ url, init });
    return new Response(null, { status: 204 });
  }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('a page on a retired address', () => {
  it('empties its origin, reports its receipt, then goes to the new address with the path only', async () => {
    const address = await load();
    address.rememberAddressReceipt(RECEIPT);
    address.handleAddressRetiredFrame(JSON.stringify({ moved_to: NEW_ORIGIN }));
    await address.leaveRetiredAddress(null);

    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(await databaseNames()).toEqual([]);
    expect(cacheKeys).toEqual([]);
    expect([unsubscribed, unregistered]).toEqual([1, 1]);
    expect(fetchCalls).toHaveLength(1);
    expect(fetchCalls[0].url).toBe('/__avibe/address-cleared');
    expect(fetchCalls[0].init).toMatchObject({ method: 'POST', body: RECEIPT, credentials: 'omit' });
    expect(replaced).toEqual([`${NEW_ORIGIN}/sessions/abc`]);
  });

  it('clears and leaves even when the report request never succeeds', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('blocked'); }));
    const address = await load();
    address.rememberAddressReceipt(RECEIPT);
    await address.leaveRetiredAddress(NEW_ORIGIN);

    expect(window.localStorage.length).toBe(0);
    expect(await databaseNames()).toEqual([]);
    expect(replaced).toEqual([`${NEW_ORIGIN}/sessions/abc`]);
  });

  it('goes to the old root and reports nothing when a store cannot be deleted', async () => {
    const original = indexedDB.deleteDatabase.bind(indexedDB);
    const spy = vi.spyOn(indexedDB, 'deleteDatabase').mockImplementation((name: string) => {
      if (name !== 'avibe-local-store') return original(name);
      const request = {} as IDBOpenDBRequest;
      queueMicrotask(() => request.onblocked?.(new Event('blocked') as IDBVersionChangeEvent));
      return request;
    });
    const address = await load();
    address.rememberAddressReceipt(RECEIPT);
    await address.leaveRetiredAddress(NEW_ORIGIN);
    spy.mockRestore();

    expect(fetchCalls).toEqual([]);
    expect(replaced).toEqual(['/']);
    expect(window.localStorage.length).toBe(0); // what could be removed still was
  });

  it('is sent only to a managed origin, whatever a frame says', async () => {
    const address = await load();
    address.handleAddressRetiredFrame(JSON.stringify({ moved_to: 'https://evil.example/', receipt: RECEIPT }));
    await address.leaveRetiredAddress(null);
    expect(replaced).toEqual(['/']);
    expect(fetchCalls[0].init.body).toBe(RECEIPT);
  });

  it('treats a retired response as the frame, after waiting for the frame when the stream is open', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    const address = await load();
    const retired = new Response(null, { status: 410, headers: { 'X-Avibe-Address': 'retired' } });
    expect(address.isRetiredAddressResponse(retired)).toBe(true);
    expect(address.isRetiredAddressResponse(new Response(null, { status: 410 }))).toBe(false);

    address.setAddressStreamProbe(() => true);
    address.noteRetiredAddressResponse();
    expect(replaced).toEqual([]);
    // The frame arrives inside the wait: it names the new address and wins.
    address.handleAddressRetiredFrame(JSON.stringify({ moved_to: NEW_ORIGIN }));
    vi.useRealTimers();
    await address.leaveRetiredAddress(null);
    expect(replaced).toEqual([`${NEW_ORIGIN}/sessions/abc`]);
  });

  it('a suspended tab with no stream clears at once and lands on the old root', async () => {
    const address = await load();
    address.rememberAddressReceipt(RECEIPT);
    address.setAddressStreamProbe(() => false);
    address.noteRetiredAddressResponse();
    await address.leaveRetiredAddress(null);
    expect(window.localStorage.length).toBe(0);
    expect(fetchCalls[0].init.body).toBe(RECEIPT);
    expect(replaced).toEqual(['/']);
  });
});
