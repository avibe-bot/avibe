// Binding an Avibe to an account stops every sign-in-less push subscription. On the computer that
// runs it, a browser takes its own back when the page is next in the foreground: no click, no
// permission prompt, and never against a choice of "off". The rows of the foreground contract in
// docs/plans/bound-network-position-handoff.md, from the page's side.
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiContextType } from '@/context/ApiContext';
import type { NativePushPendingRegistration, NativePushPendingRegistrationStore } from './webPush';

const OLD = 'https://push.example.test/sub/old';
const FRESH = 'https://push.example.test/sub/fresh';

const load = async () => {
  vi.resetModules();
  return import('./webPush');
};

type Subscription = {
  endpoint: string;
  options: { applicationServerKey: ArrayBuffer };
  unsubscribe: ReturnType<typeof vi.fn>;
  toJSON: () => PushSubscriptionJSON;
};

function browser({
  hostname = '127.0.0.1',
  permission = 'granted',
  live = false,
  remembered = [OLD],
}: { hostname?: string; permission?: NotificationPermission; live?: boolean; remembered?: string[] } = {}) {
  let current: Subscription | null = null;
  const at = (endpoint: string): Subscription => ({
    endpoint,
    options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
    unsubscribe: vi.fn(async () => {
      current = null;
      return true;
    }),
    toJSON: () => ({ endpoint, keys: { p256dh: `key-${endpoint}`, auth: `auth-${endpoint}` } }),
  });
  const old = at(OLD);
  const fresh = at(FRESH);
  if (live) current = old;
  const pushManager = {
    getSubscription: vi.fn(async () => current),
    subscribe: vi.fn(async () => {
      current = fresh;
      return fresh;
    }),
  };
  const stored = new Map<string, string>([
    ['vibe.webPush.deviceId', 'device-1'],
    ['vibe.webPush.endpoints', JSON.stringify(remembered)],
  ]);
  const requestPermission = vi.fn(async () => permission);
  vi.stubGlobal('window', {
    PushManager: class {},
    Notification: { permission, requestPermission },
    localStorage: {
      getItem: (key: string) => stored.get(key) ?? null,
      setItem: (key: string, value: string) => stored.set(key, value),
      removeItem: (key: string) => stored.delete(key),
    },
    location: { hostname, origin: `http://${hostname}:5123` },
    matchMedia: () => ({ matches: false }),
    atob: (value: string) => Buffer.from(value, 'base64').toString('binary'),
  });
  vi.stubGlobal('navigator', {
    platform: 'MacIntel',
    maxTouchPoints: 0,
    userAgent: '',
    serviceWorker: {
      getRegistration: vi.fn(async () => ({ pushManager })),
      register: vi.fn(async () => ({ pushManager })),
    },
  });
  vi.stubGlobal('Notification', window.Notification);
  return { old, fresh, pushManager, requestPermission, stored };
}

/** The instance: `retired` endpoints are the ones binding stopped and this browser may take back. */
function instance({ retired = [OLD], accepts = true }: { retired?: string[]; accepts?: boolean } = {}) {
  const getWebPushStatus = vi.fn(async (payload?: { endpoint?: string }) => ({
    ok: true,
    configured: true,
    public_key: 'AQIDBA',
    subscription_count: 0,
    current_subscription_enabled: false,
    current_subscription_repairable: false,
    current_subscription_binding_retired: Boolean(payload?.endpoint && retired.includes(payload.endpoint)),
  }));
  const subscribeWebPush = vi.fn(async () => ({ ok: true, accepted: accepts, subscription: null }));
  const unsubscribeWebPush = vi.fn(async () => ({ ok: true, disabled: true }));
  const api = { getWebPushStatus, subscribeWebPush, unsubscribeWebPush } as unknown as ApiContextType;
  return { api, getWebPushStatus, subscribeWebPush, unsubscribeWebPush };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('a browser on the computer that runs Avibe, after binding', () => {
  it('takes back its live subscription as it is, without a prompt', async () => {
    const page = browser({ live: true });
    const { api, getWebPushStatus, subscribeWebPush } = instance();
    const { restoreWebPushAfterBinding } = await load();

    await expect(restoreWebPushAfterBinding(api)).resolves.toBe(true);

    expect(getWebPushStatus).toHaveBeenCalledWith({
      endpoint: OLD,
      subscription: page.old.toJSON(),
      device_id: 'device-1',
      previous_endpoints: [OLD],
    });
    // Same endpoint with its own keys, the device, the repair intent; never a background rotation.
    expect(subscribeWebPush.mock.calls).toEqual([
      [page.old.toJSON(), undefined, 'device-1', undefined, undefined, 'binding_repair'],
    ]);
    expect(page.requestPermission).not.toHaveBeenCalled();
    expect(page.pushManager.subscribe).not.toHaveBeenCalled();
  });

  it('subscribes again when the browser lost its subscription, naming the endpoint it remembers', async () => {
    const page = browser({ live: false });
    const { api, getWebPushStatus, subscribeWebPush } = instance();
    const { restoreWebPushAfterBinding, getRememberedWebPushEndpoints } = await load();

    await expect(restoreWebPushAfterBinding(api)).resolves.toBe(true);

    expect(getWebPushStatus).toHaveBeenCalledWith({ endpoint: OLD });
    expect(subscribeWebPush.mock.calls).toEqual([
      [page.fresh.toJSON(), undefined, 'device-1', [OLD], undefined, 'binding_repair'],
    ]);
    expect(page.requestPermission).not.toHaveBeenCalled();
    expect(page.fresh.unsubscribe).not.toHaveBeenCalled();
    expect((await getRememberedWebPushEndpoints())[0]).toBe(FRESH);
  });

  it.each([
    ['the instance refuses (it was turned off at the same moment)', () => instance({ accepts: false })],
    ['the answer never comes', () => {
      const made = instance();
      made.subscribeWebPush.mockRejectedValue(new Error('offline'));
      return made;
    }],
  ])('removes the subscription it made for the attempt when %s, and posts nothing else', async (_label, make) => {
    const page = browser({ live: false });
    const { api, subscribeWebPush } = make();
    const { restoreWebPushAfterBinding } = await load();

    await expect(restoreWebPushAfterBinding(api)).resolves.toBe(false);

    expect(subscribeWebPush).toHaveBeenCalledOnce();
    expect(subscribeWebPush.mock.calls[0][5]).toBe('binding_repair');
    expect(page.fresh.unsubscribe).toHaveBeenCalledOnce();
  });

  it('leaves a live subscription alone when the instance refuses it', async () => {
    const page = browser({ live: true });
    const { api, subscribeWebPush } = instance({ accepts: false });
    const { restoreWebPushAfterBinding } = await load();

    await expect(restoreWebPushAfterBinding(api)).resolves.toBe(false);

    expect(subscribeWebPush).toHaveBeenCalledOnce();
    expect(page.old.unsubscribe).not.toHaveBeenCalled();
  });

  it.each([
    ['notifications are blocked in the browser', { permission: 'denied' as const }],
    ['the browser was never asked', { permission: 'default' as const }],
    ['the page is open at the account\'s own address (a signed-in device)', { hostname: 'alex.avibe.bot' }],
    ['the page is open at a network address', { hostname: '192.168.1.20' }],
  ])('sends nothing and asks nothing when %s', async (_label, where) => {
    const page = browser({ live: true, ...where });
    const { api, getWebPushStatus, subscribeWebPush } = instance();
    const { restoreWebPushAfterBinding } = await load();

    await expect(restoreWebPushAfterBinding(api)).resolves.toBe(false);

    expect(getWebPushStatus).not.toHaveBeenCalled();
    expect(subscribeWebPush).not.toHaveBeenCalled();
    expect(page.requestPermission).not.toHaveBeenCalled();
    expect(page.pushManager.subscribe).not.toHaveBeenCalled();
  });

  it('posts nothing for a subscription the instance does not offer back (turned off, or never stopped)', async () => {
    const page = browser({ live: false });
    const { api, getWebPushStatus, subscribeWebPush } = instance({ retired: [] });
    const { restoreWebPushAfterBinding } = await load();

    await expect(restoreWebPushAfterBinding(api)).resolves.toBe(false);

    expect(getWebPushStatus).toHaveBeenCalledOnce();
    expect(subscribeWebPush).not.toHaveBeenCalled();
    expect(page.pushManager.subscribe).not.toHaveBeenCalled();
  });

  it('lets a press on "off" finish first when both happen at once, and then stays off', async () => {
    const page = browser({ live: true });
    const { api, getWebPushStatus, subscribeWebPush, unsubscribeWebPush } = instance();
    let recorded: (() => void) | undefined;
    const retired = [OLD];
    unsubscribeWebPush.mockImplementation(() => new Promise((resolve) => {
      recorded = () => {
        retired.length = 0; // the instance has the opt-out: nothing is offered back any more
        resolve({ ok: true, disabled: true });
      };
    }));
    getWebPushStatus.mockImplementation(async (payload?: { endpoint?: string }) => ({
      ok: true,
      configured: true,
      public_key: 'AQIDBA',
      subscription_count: 0,
      current_subscription_binding_retired: Boolean(payload?.endpoint && retired.includes(payload.endpoint)),
    }));
    const { disableWebPush, restoreWebPushAfterBinding } = await load();

    const off = disableWebPush(api);
    const back = restoreWebPushAfterBinding(api);
    await vi.waitFor(() => expect(recorded).toBeDefined());
    // The page asks about its subscription only after the opt-out is recorded.
    expect(getWebPushStatus).not.toHaveBeenCalled();
    recorded?.();

    await expect(off).resolves.toMatchObject({ status: 'disabled' });
    await expect(back).resolves.toBe(false);
    expect(unsubscribeWebPush).toHaveBeenCalledWith(OLD, 'device-1');
    expect(subscribeWebPush).not.toHaveBeenCalled();
    expect(page.old.unsubscribe).toHaveBeenCalledOnce();
  });

  it('shows the switch on, not off for a moment, where the page reads the state', async () => {
    browser({ live: true });
    const made = instance();
    const { reconcileWebPush, readWebPushDeviceState } = await load();

    // The notification control.
    await expect(reconcileWebPush(made.api)).resolves.toMatchObject({ status: 'enabled' });
    // Home's setup list.
    await expect(readWebPushDeviceState(made.api)).resolves.toBe('on');
    expect(made.subscribeWebPush.mock.calls.every((call) => call[5] === 'binding_repair')).toBe(true);
  });

  it('reads as off where the page reads the state and the browser no longer allows notifications', async () => {
    const page = browser({ live: true, permission: 'denied' });
    const made = instance();
    const { reconcileWebPush, readWebPushDeviceState } = await load();

    await expect(reconcileWebPush(made.api)).resolves.toMatchObject({ status: 'disabled' });
    await expect(readWebPushDeviceState(made.api)).resolves.toBe('off');
    expect(made.subscribeWebPush).not.toHaveBeenCalled();
    expect(page.requestPermission).not.toHaveBeenCalled();
  });
});

describe('a native registration that is waiting for the instance', () => {
  const ENDPOINT = 'https://avibe.bot/api/push/apns/native-current';
  const waiting = (intent?: unknown): NativePushPendingRegistration => ({
    endpoint: ENDPOINT,
    keys: { p256dh: 'key', auth: 'auth' },
    previousEndpoints: [ENDPOINT],
    vapidPublicKey: 'AQIDBA',
    ...(intent === undefined ? {} : { intent }),
  } as NativePushPendingRegistration);

  function app(status: Record<string, unknown>, saved: Record<string, string> = {}) {
    const stored = new Map<string, string>(Object.entries(saved));
    vi.stubGlobal('window', {
      avibeNative: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'test',
        capabilities: { push: 1 },
        call: vi.fn(async (method: string) => {
          if (method === 'push.status') return status;
          if (method === 'push.subscribe') return { endpoint: ENDPOINT, keys: { p256dh: 'key', auth: 'auth' } };
          throw new Error(`unexpected method ${method}`);
        }),
        on: vi.fn(() => () => undefined),
      },
      localStorage: {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => stored.set(key, value),
        removeItem: (key: string) => stored.delete(key),
      },
    });
    return stored;
  }

  function memoryStore(value: NativePushPendingRegistration | null = null) {
    const store: NativePushPendingRegistrationStore & { value: NativePushPendingRegistration | null } = {
      value,
      read: () => store.value,
      write: (registration) => {
        store.value = registration;
      },
      clear: () => {
        store.value = null;
      },
    };
    return store;
  }

  const on = { state: 'on', endpoint: ENDPOINT, keys: { p256dh: 'key', auth: 'auth' }, permission: 'granted' };
  const subscription = { endpoint: ENDPOINT, keys: { p256dh: 'key', auth: 'auth' } };

  it('keeps the press it came from when the first post did not arrive', async () => {
    app({ state: 'off' });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn().mockRejectedValue(new Error('offline')),
    } as unknown as ApiContextType;
    const store = memoryStore();
    const { enableWebPush } = await load();

    await expect(enableWebPush(api, { pendingRegistrationStore: store })).rejects.toThrow('offline');

    expect(store.value?.intent).toBe('explicit_enable');
  });

  it.each([
    ['a press', 'explicit_enable', [subscription, 'iPhone App', undefined, [ENDPOINT], undefined, 'explicit_enable']],
    ['a repair after binding', 'binding_repair', [subscription, 'iPhone App', undefined, [ENDPOINT], undefined, 'binding_repair']],
    ['nothing recorded', undefined, [subscription, 'iPhone App', undefined, [ENDPOINT], undefined]],
  ])('is posted again as %s', async (_label, intent, posted) => {
    app(on);
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
    } as unknown as ApiContextType;
    const store = memoryStore(waiting(intent));
    const { reconcileWebPush } = await load();

    await expect(reconcileWebPush(api, store)).resolves.toMatchObject({ status: 'enabled' });

    expect(vi.mocked(api.subscribeWebPush).mock.calls).toEqual([posted]);
    expect(store.value).toBeNull();
  });

  it.each([
    ['a press', 'explicit_enable', 'explicit_enable'],
    ['a value this page does not know', 'enable_everything', undefined],
    ['nothing (written by an earlier version)', undefined, undefined],
  ])('read back from the App\'s storage as %s, is posted as that and never as more', async (_label, saved, posted) => {
    app(on, { 'vibe.nativePush.pendingRegistration': JSON.stringify(waiting(saved)) });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
    } as unknown as ApiContextType;
    const { reconcileWebPush } = await load();

    await expect(reconcileWebPush(api)).resolves.toMatchObject({ status: 'enabled' });

    expect(vi.mocked(api.subscribeWebPush)).toHaveBeenCalledOnce();
    expect(vi.mocked(api.subscribeWebPush).mock.calls[0][5]).toBe(posted);
  });

  it('is dropped when the instance refuses a repair, so that no later post can create a registration', async () => {
    app(on);
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: false })),
    } as unknown as ApiContextType;
    const store = memoryStore(waiting('binding_repair'));
    const { reconcileWebPush } = await load();

    await expect(reconcileWebPush(api, store)).resolves.toMatchObject({ status: 'disabled' });

    expect(api.subscribeWebPush).toHaveBeenCalledOnce();
    expect(store.value).toBeNull();
  });

  it.each([
    ['still allows notifications', 'granted', 'enabled', 1],
    ['no longer allows them', 'denied', 'disabled', 0],
    ['does not say (an older App)', undefined, 'disabled', 0],
  ])('takes back a subscription binding stopped only while the App %s', async (_label, permission, status, posts) => {
    app({ ...on, permission });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn(async () => ({
        current_subscription_enabled: false,
        current_subscription_repairable: false,
        current_subscription_binding_retired: true,
      })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
    } as unknown as ApiContextType;
    const { reconcileWebPush } = await load();

    await expect(reconcileWebPush(api, memoryStore())).resolves.toMatchObject({ status });

    expect(vi.mocked(api.subscribeWebPush).mock.calls).toEqual(
      posts ? [[subscription, 'iPhone App', undefined, [], undefined, 'binding_repair']] : [],
    );
  });
});
