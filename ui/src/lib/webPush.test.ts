import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiContextType } from '@/context/ApiContext';
import { enableWebPush } from './webPush';

describe('web push recovery', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('forces a fresh browser subscription while preserving the old endpoint for cleanup', async () => {
    const oldSubscription = {
      endpoint: 'https://push.example.test/sub/old',
      options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
      unsubscribe: vi.fn(async () => {
        currentSubscription = null;
        return true;
      }),
      toJSON: () => ({
        endpoint: 'https://push.example.test/sub/old',
        keys: { p256dh: 'old-key', auth: 'old-auth' },
      }),
    };
    const newSubscription = {
      endpoint: 'https://push.example.test/sub/new',
      options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
      unsubscribe: vi.fn(),
      toJSON: () => ({
        endpoint: 'https://push.example.test/sub/new',
        keys: { p256dh: 'new-key', auth: 'new-auth' },
      }),
    };
    let currentSubscription: typeof oldSubscription | typeof newSubscription | null = oldSubscription;
    const registration = {
      pushManager: {
        getSubscription: vi.fn(async () => currentSubscription),
        subscribe: vi.fn(async () => {
          currentSubscription = newSubscription;
          return newSubscription;
        }),
      },
    };
    const localStorage = new Map<string, string>();
    const endpointCache = { put: vi.fn(async () => undefined) };
    vi.stubGlobal('window', {
      PushManager: class {},
      Notification: { requestPermission: vi.fn(async () => 'granted'), permission: 'granted' },
      crypto: { randomUUID: () => 'device-1' },
      localStorage: {
        getItem: (key: string) => localStorage.get(key) ?? null,
        setItem: (key: string, value: string) => localStorage.set(key, value),
      },
      caches: { open: vi.fn(async () => endpointCache) },
      location: { origin: 'https://avibe.local' },
      matchMedia: () => ({ matches: false }),
      atob: (value: string) => Buffer.from(value, 'base64').toString('binary'),
    });
    vi.stubGlobal('navigator', {
      platform: 'MacIntel',
      maxTouchPoints: 0,
      userAgent: '',
      serviceWorker: { register: vi.fn(async () => registration) },
    });
    vi.stubGlobal('Notification', window.Notification);

    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true })),
    } as unknown as ApiContextType;

    await enableWebPush(api, { forceResubscribe: true });

    expect(oldSubscription.unsubscribe).toHaveBeenCalledOnce();
    expect(registration.pushManager.subscribe).toHaveBeenCalledOnce();
    expect(api.subscribeWebPush).toHaveBeenCalledWith(
      newSubscription.toJSON(),
      undefined,
      'device-1',
      ['https://push.example.test/sub/old'],
    );
    expect(endpointCache.put).toHaveBeenCalledWith(
      'https://avibe.local/__avibe/web-push-endpoint',
      expect.any(Response),
    );
    expect(await endpointCache.put.mock.calls[0][1].json()).toEqual({
      endpoint: 'https://push.example.test/sub/new',
    });
  });

  it('does not re-enable an invalid endpoint when the browser refuses to unsubscribe', async () => {
    const oldSubscription = {
      endpoint: 'https://push.example.test/sub/old',
      options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
      unsubscribe: vi.fn(async () => false),
    };
    const registration = {
      pushManager: {
        getSubscription: vi.fn(async () => oldSubscription),
        subscribe: vi.fn(),
      },
    };
    vi.stubGlobal('window', {
      PushManager: class {},
      Notification: { requestPermission: vi.fn(async () => 'granted') },
      matchMedia: () => ({ matches: false }),
      atob: (value: string) => Buffer.from(value, 'base64').toString('binary'),
    });
    vi.stubGlobal('navigator', {
      platform: 'MacIntel',
      maxTouchPoints: 0,
      userAgent: '',
      serviceWorker: { register: vi.fn(async () => registration) },
    });
    vi.stubGlobal('Notification', window.Notification);
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(enableWebPush(api, { forceResubscribe: true })).rejects.toThrow('unsubscribe_failed');
    expect(oldSubscription.unsubscribe).toHaveBeenCalledOnce();
    expect(registration.pushManager.subscribe).not.toHaveBeenCalled();
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });
});
