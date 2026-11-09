import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiContextType } from '@/context/ApiContext';
import {
  disableWebPush,
  enableWebPush,
  reconcileWebPush,
  webPushSubscriptionUsesVapidKey,
} from './webPush';

describe('web push recovery', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('detects a subscription bound to an obsolete VAPID key', () => {
    vi.stubGlobal('window', {
      atob: (value: string) => Buffer.from(value, 'base64').toString('binary'),
    });
    const subscription = {
      options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
    } as PushSubscription;

    expect(webPushSubscriptionUsesVapidKey(subscription, 'AQIDBA')).toBe(true);
    expect(webPushSubscriptionUsesVapidKey(subscription, 'AQIDBQ')).toBe(false);
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
    const endpointCache = {
      match: vi.fn(async () => undefined),
      put: vi.fn(async (_url: string, _response: Response) => undefined),
    };
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
      undefined,
    );
    expect(endpointCache.put).toHaveBeenCalledWith(
      'https://avibe.local/__avibe/web-push-endpoint',
      expect.any(Response),
    );
    const endpointWrite = endpointCache.put.mock.calls.find(
      ([url]) => url === 'https://avibe.local/__avibe/web-push-endpoint',
    );
    expect(endpointWrite).toBeDefined();
    expect(await endpointWrite![1].json()).toEqual({
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

  it('sends the device ID when disabling a browser subscription', async () => {
    const subscription = {
      endpoint: 'https://push.example.test/sub/old',
      unsubscribe: vi.fn(async () => true),
    };
    vi.stubGlobal('window', {
      localStorage: { getItem: () => 'device-1' },
    });
    vi.stubGlobal('navigator', {
      serviceWorker: {
        getRegistration: vi.fn(async () => ({
          pushManager: { getSubscription: vi.fn(async () => subscription) },
        })),
      },
    });
    const api = {
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
    } as unknown as ApiContextType;

    expect(await disableWebPush(api)).toBe(true);
    expect(subscription.unsubscribe).toHaveBeenCalledOnce();
    expect(api.unsubscribeWebPush).toHaveBeenCalledWith(subscription.endpoint, 'device-1');
  });

  it('passes the current VAPID key when disabling a native subscription', async () => {
    vi.stubGlobal('window', {
      avibeNative: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'test',
        capabilities: { push: 1 },
        call: vi.fn(async (method: string, params: Record<string, unknown>) => {
          if (method === 'push.status') {
            expect(params).toEqual({ vapidPublicKey: 'AQIDBA' });
            return {
              state: 'on',
              endpoint: 'https://avibe.bot/api/push/apns/registration',
              keys: { p256dh: 'key', auth: 'auth' },
            };
          }
          if (method === 'push.unsubscribe') return { revoked: true };
          throw new Error(`unexpected method ${method}`);
        }),
        on: vi.fn(() => () => undefined),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
    } as unknown as ApiContextType;

    expect(await disableWebPush(api)).toBe(true);
    expect(api.getWebPushVapidPublicKey).toHaveBeenCalledOnce();
    expect(api.unsubscribeWebPush).toHaveBeenCalledWith(
      'https://avibe.bot/api/push/apns/registration',
    );
  });

  it('re-posts an existing native subscription when the instance is disabled', async () => {
    vi.stubGlobal('window', {
      avibeNative: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'test',
        capabilities: { push: 1 },
        call: vi.fn(async (method: string, params: Record<string, unknown>) => {
          if (method === 'push.status') {
            expect(params).toEqual({ vapidPublicKey: 'AQIDBA' });
            return {
              state: 'on',
              endpoint: 'https://avibe.bot/api/push/apns/registration',
              keys: { p256dh: 'key', auth: 'auth' },
            };
          }
          throw new Error(`unexpected method ${method}`);
        }),
        on: vi.fn(() => () => undefined),
      },
      localStorage: { getItem: () => null, setItem: vi.fn() },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
    } as unknown as ApiContextType;

    await expect(enableWebPush(api)).resolves.toEqual({
      endpoint: 'https://avibe.bot/api/push/apns/registration',
      keys: { p256dh: 'key', auth: 'auth' },
    });
    expect(api.subscribeWebPush).toHaveBeenCalledWith(
      {
        endpoint: 'https://avibe.bot/api/push/apns/registration',
        keys: { p256dh: 'key', auth: 'auth' },
      },
      'iPhone App',
      undefined,
      ['https://avibe.bot/api/push/apns/registration'],
      undefined,
    );
  });

  it('reconciles a native endpoint again after the instance disables it', async () => {
    let nativeEndpoint = 'https://avibe.bot/api/push/apns/native-old';
    const nativeSubscription = () => ({
      state: 'on' as const,
      endpoint: nativeEndpoint,
      keys: { p256dh: 'key', auth: 'auth' },
    });
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') return nativeSubscription();
      if (method === 'push.subscribe') {
        nativeEndpoint = 'https://avibe.bot/api/push/apns/native-new';
        return {
          endpoint: nativeEndpoint,
          keys: { p256dh: 'new-key', auth: 'new-auth' },
        };
      }
      throw new Error(`unexpected method ${method}`);
    });
    vi.stubGlobal('window', {
      avibeNative: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'test',
        capabilities: { push: 1 },
        call,
        on: vi.fn(() => () => undefined),
      },
      localStorage: { getItem: () => null, setItem: vi.fn() },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn()
        .mockResolvedValueOnce({
          current_subscription_enabled: false,
          current_subscription_repairable: true,
        })
        .mockResolvedValueOnce({
          current_subscription_enabled: true,
          current_subscription_repairable: false,
        }),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
    } as unknown as ApiContextType;

    await expect(reconcileWebPush(api)).resolves.toMatchObject({
      status: 'enabled',
    });
    expect(api.subscribeWebPush).toHaveBeenCalledWith(
      {
        endpoint: 'https://avibe.bot/api/push/apns/native-new',
        keys: { p256dh: 'new-key', auth: 'new-auth' },
      },
      'iPhone App',
      undefined,
      ['https://avibe.bot/api/push/apns/native-old'],
      true,
    );
  });

  it('shares one in-flight reconciliation between load and foreground', async () => {
    let releaseKey: (() => void) | undefined;
    const keyReady = new Promise<void>((resolve) => {
      releaseKey = resolve;
    });
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return { state: 'off' as const };
      }
      throw new Error(`unexpected method ${method}`);
    });
    vi.stubGlobal('window', {
      avibeNative: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'test',
        capabilities: { push: 1 },
        call,
        on: vi.fn(() => () => undefined),
      },
      localStorage: { getItem: () => null, setItem: vi.fn() },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => {
        await keyReady;
        return { public_key: 'AQIDBA' };
      }),
    } as unknown as ApiContextType;

    const first = reconcileWebPush(api);
    const second = reconcileWebPush(api);

    expect(second).toBe(first);
    expect(api.getWebPushVapidPublicKey).toHaveBeenCalledOnce();
    releaseKey?.();
    await expect(Promise.all([first, second])).resolves.toEqual([
      {
        support: { supported: true, standalone: true, requiresStandalone: false },
        status: 'disabled',
      },
      {
        support: { supported: true, standalone: true, requiresStandalone: false },
        status: 'disabled',
      },
    ]);
    expect(call).toHaveBeenCalledOnce();
  });

  it('keeps the browser subscription when the server cannot record an opt-out', async () => {
    const subscription = {
      endpoint: 'https://push.example.test/sub/old',
      unsubscribe: vi.fn(async () => true),
    };
    vi.stubGlobal('navigator', {
      serviceWorker: {
        getRegistration: vi.fn(async () => ({
          pushManager: { getSubscription: vi.fn(async () => subscription) },
        })),
      },
    });
    const api = {
      unsubscribeWebPush: vi.fn(async () => { throw new Error('offline'); }),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api)).rejects.toThrow('offline');
    expect(subscription.unsubscribe).not.toHaveBeenCalled();
  });

  it('recovers the same device and confirmed endpoint after a reload with localStorage blocked', async () => {
    const entries = new Map<string, Response>();
    const cache = {
      match: vi.fn(async (url: string) => entries.get(url)?.clone()),
      put: vi.fn(async (url: string, response: Response) => {
        entries.set(url, response.clone());
      }),
    };
    const randomUUID = vi.fn()
      .mockReturnValueOnce('device-1')
      .mockReturnValue('unexpected-new-device');
    let pendingLock = Promise.resolve();
    const locks = {
      request: vi.fn((_name: string, callback: () => Promise<string>) => {
        const result = pendingLock.then(callback);
        pendingLock = result.then(() => undefined);
        return result;
      }),
    };
    vi.stubGlobal('window', {
      localStorage: {
        getItem: () => { throw new Error('blocked'); },
        setItem: () => { throw new Error('blocked'); },
      },
      caches: { open: vi.fn(async () => cache) },
      crypto: { randomUUID },
      location: { origin: 'https://avibe.local' },
    });
    vi.stubGlobal('navigator', { locks });

    vi.resetModules();
    const firstPage = await import('./webPush');
    vi.resetModules();
    const secondPage = await import('./webPush');
    expect(await Promise.all([
      firstPage.getWebPushDeviceId(),
      secondPage.getWebPushDeviceId(),
    ])).toEqual(['device-1', 'device-1']);
    expect(locks.request).toHaveBeenCalledTimes(2);
    await firstPage.rememberWebPushEndpoint('https://push.example.test/sub/old');

    vi.resetModules();
    const nextPage = await import('./webPush');
    expect(await nextPage.getWebPushDeviceId()).toBe('device-1');
    expect(await nextPage.getRememberedWebPushEndpoints()).toEqual([
      'https://push.example.test/sub/old',
    ]);
    expect(randomUUID).toHaveBeenCalledOnce();
  });

  it('does not re-enable a subscription when conditional recovery is rejected', async () => {
    const subscription = {
      endpoint: 'https://push.example.test/sub/new',
      options: { applicationServerKey: new Uint8Array([1, 2, 3, 4]).buffer },
      unsubscribe: vi.fn(async () => true),
      toJSON: () => ({ endpoint: 'https://push.example.test/sub/new' }),
    };
    vi.stubGlobal('window', {
      PushManager: class {},
      Notification: { requestPermission: vi.fn(async () => 'granted') },
      localStorage: { getItem: () => null, setItem: vi.fn() },
      crypto: { randomUUID: () => 'device-1' },
      matchMedia: () => ({ matches: false }),
      atob: (value: string) => Buffer.from(value, 'base64').toString('binary'),
    });
    vi.stubGlobal('navigator', {
      platform: 'MacIntel',
      maxTouchPoints: 0,
      userAgent: '',
      serviceWorker: {
        register: vi.fn(async () => ({
          pushManager: {
            getSubscription: vi.fn(async () => null),
            subscribe: vi.fn(async () => subscription),
          },
        })),
      },
    });
    vi.stubGlobal('Notification', window.Notification);
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: false, subscription: null })),
    } as unknown as ApiContextType;

    await expect(enableWebPush(api, { recoverOnly: true })).rejects.toThrow('recovery_not_authorized');
    expect(api.subscribeWebPush).toHaveBeenCalledWith(
      subscription.toJSON(),
      undefined,
      'device-1',
      [],
      true,
    );
    expect(subscription.unsubscribe).toHaveBeenCalledOnce();
  });
});
