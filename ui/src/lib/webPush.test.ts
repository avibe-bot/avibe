import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiContextType } from '@/context/ApiContext';
import {
  createNativePushDisabledIntentStore,
  disableWebPush,
  enableWebPush,
  type NativePushDisabledIntentStore,
  type NativePushPendingRegistration,
  type NativePushPendingRegistrationStore,
  readWebPushDeviceState,
  reconcileWebPush,
  webPushSubscriptionUsesVapidKey,
} from './webPush';

function pendingRegistrationStore(): NativePushPendingRegistrationStore & {
  value: NativePushPendingRegistration | null;
} {
  return {
    value: null,
    read() {
      return this.value;
    },
    write(registration) {
      this.value = registration;
    },
    clear() {
      this.value = null;
    },
  };
}

function disabledIntentStore(
  initial = false,
): NativePushDisabledIntentStore & {
  value: boolean;
  cleanup: 'pending' | 'failed' | 'confirmed' | null;
} {
  return {
    value: initial,
    cleanup: initial ? 'pending' : null,
    isDisabled() {
      return this.cleanup !== null;
    },
    cleanupStatus() {
      return this.cleanup;
    },
    disable() {
      this.value = true;
      this.cleanup = 'pending';
    },
    setCleanupStatus(status) {
      this.value = true;
      this.cleanup = status;
    },
    clear() {
      this.value = false;
      this.cleanup = null;
    },
  };
}

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    values,
    storage: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    },
  };
}

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

    await expect(disableWebPush(api)).resolves.toMatchObject({
      status: 'disabled',
    });
    expect(subscription.unsubscribe).toHaveBeenCalledOnce();
    expect(api.unsubscribeWebPush).toHaveBeenCalledWith(subscription.endpoint, 'device-1');
  });

  it('passes the current VAPID key when disabling a native subscription', async () => {
    const localStorage = new Map<string, string>();
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
      localStorage: {
        getItem: (key: string) => localStorage.get(key) ?? null,
        setItem: (key: string, value: string) => localStorage.set(key, value),
        removeItem: (key: string) => localStorage.delete(key),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api)).resolves.toMatchObject({
      status: 'disabled',
    });
    expect(api.getWebPushVapidPublicKey).toHaveBeenCalledOnce();
    expect(api.unsubscribeWebPush).toHaveBeenCalledWith(
      'https://avibe.bot/api/push/apns/registration',
    );
  });

  it('preserves an explicit native disable during foreground reconciliation', async () => {
    const localStorage = new Map<string, string>();
    let enabled = false;
    const call = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === 'push.status') {
        expect(params).toEqual({ vapidPublicKey: 'AQIDBA' });
        return enabled
          ? {
              state: 'on' as const,
              endpoint: 'https://avibe.bot/api/push/apns/native-old',
              keys: { p256dh: 'old-key', auth: 'old-auth' },
            }
          : { state: 'off' as const };
      }
      if (method === 'push.subscribe') {
        enabled = true;
        return {
          endpoint: 'https://avibe.bot/api/push/apns/native-old',
          keys: { p256dh: 'old-key', auth: 'old-auth' },
        };
      }
      if (method === 'push.unsubscribe') {
        enabled = false;
        return { revoked: true };
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
      localStorage: {
        getItem: (key: string) => localStorage.get(key) ?? null,
        setItem: (key: string, value: string) => {
          localStorage.set(key, value);
        },
        removeItem: (key: string) => {
          localStorage.delete(key);
        },
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
      getWebPushStatus: vi.fn(async () => ({
        current_subscription_enabled: false,
        current_subscription_repairable: false,
      })),
      subscribeWebPush: vi.fn(async () => ({
        ok: true,
        accepted: true,
      })),
    } as unknown as ApiContextType;

    await expect(enableWebPush(api)).resolves.toMatchObject({
      endpoint: 'https://avibe.bot/api/push/apns/native-old',
    });
    await expect(disableWebPush(api)).resolves.toMatchObject({
      status: 'disabled',
    });
    await expect(
      reconcileWebPush(api, pendingRegistrationStore()),
    ).resolves.toMatchObject({ status: 'disabled' });

    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'push.status',
      'push.subscribe',
      'push.status',
      'push.unsubscribe',
    ]);
    expect(JSON.parse(localStorage.get('vibe.nativePush.disabledIntent') ?? '{}'))
      .toEqual({ cleanup: 'confirmed' });
    expect(api.getWebPushStatus).not.toHaveBeenCalled();
    expect(api.subscribeWebPush).toHaveBeenCalledOnce();
  });

  it('does not replay a pending native registration after an explicit opt-out', async () => {
    const disabled = disabledIntentStore(true);
    const pending = pendingRegistrationStore();
    pending.write({
      endpoint: 'https://avibe.bot/api/push/apns/native-current',
      keys: { p256dh: 'key', auth: 'auth' },
      previousEndpoints: [],
      vapidPublicKey: 'AQIDBA',
    });
    const call = vi.fn();
    vi.stubGlobal('window', {
      avibeNative: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'test',
        capabilities: { push: 1 },
        call,
        on: vi.fn(() => () => undefined),
      },
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(reconcileWebPush(api, pending, disabled)).resolves.toMatchObject({
      status: 'disable_pending',
    });

    expect(call).not.toHaveBeenCalled();
    expect(api.getWebPushVapidPublicKey).not.toHaveBeenCalled();
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
    expect(pending.value).not.toBeNull();
  });

  it('reports a deferred native revoke without allowing automatic recovery', async () => {
    const disabled = disabledIntentStore();
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          endpoint: 'https://avibe.bot/api/push/apns/native-old',
          keys: { p256dh: 'key', auth: 'auth' },
        };
      }
      if (method === 'push.unsubscribe') return { revoked: false };
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'pending',
      remoteRevoked: true,
      nativeRevoked: 'pending',
    });
    await expect(reconcileWebPush(api, pendingRegistrationStore(), disabled))
      .resolves.toMatchObject({ status: 'disable_pending' });

    expect(disabled.value).toBe(true);
    expect(disabled.cleanup).toBe('pending');
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it('reports a bridge failure after instance deletion without allowing recovery', async () => {
    const disabled = disabledIntentStore();
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          endpoint: 'https://avibe.bot/api/push/apns/native-old',
          keys: { p256dh: 'key', auth: 'auth' },
        };
      }
      if (method === 'push.unsubscribe') throw new Error('bridge unavailable');
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'failed',
      remoteRevoked: true,
      nativeRevoked: 'failed',
    });
    await expect(reconcileWebPush(api, pendingRegistrationStore(), disabled))
      .resolves.toMatchObject({ status: 'disable_failed' });

    expect(disabled.value).toBe(true);
    expect(disabled.cleanup).toBe('failed');
    expect(api.unsubscribeWebPush).toHaveBeenCalledOnce();
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it('keeps the opt-out when the instance deletion fails', async () => {
    const disabled = disabledIntentStore();
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          endpoint: 'https://avibe.bot/api/push/apns/native-old',
          keys: { p256dh: 'key', auth: 'auth' },
        };
      }
      if (method === 'push.unsubscribe') return { revoked: true };
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(async () => {
        throw new Error('offline');
      }),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'failed',
      remoteRevoked: false,
      nativeRevoked: 'confirmed',
    });
    await expect(reconcileWebPush(api, pendingRegistrationStore(), disabled))
      .resolves.toMatchObject({ status: 'disable_failed' });

    expect(disabled.cleanup).toBe('failed');
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it('releases the native opt-out only through explicit enable', async () => {
    const disabled = disabledIntentStore(true);
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') return { state: 'off' as const };
      if (method === 'push.subscribe') {
        return {
          endpoint: 'https://avibe.bot/api/push/apns/native-new',
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
    } as unknown as ApiContextType;

    await expect(enableWebPush(api, { disabledIntentStore: disabled })).resolves.toMatchObject({
      endpoint: 'https://avibe.bot/api/push/apns/native-new',
    });

    expect(disabled.value).toBe(false);
    expect(api.subscribeWebPush).toHaveBeenCalledOnce();
  });

  it('does not acknowledge a native disable when its intent cannot be persisted', async () => {
    const disabled: NativePushDisabledIntentStore = {
      isDisabled: () => false,
      cleanupStatus: () => null,
      disable: () => { throw new Error('storage blocked'); },
      setCleanupStatus: vi.fn(),
      clear: vi.fn(),
    };
    const call = vi.fn();
    vi.stubGlobal('window', {
      avibeNative: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'test',
        capabilities: { push: 1 },
        call,
        on: vi.fn(() => () => undefined),
      },
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(),
      unsubscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).rejects.toThrow('storage blocked');
    expect(call).not.toHaveBeenCalled();
    expect(api.unsubscribeWebPush).not.toHaveBeenCalled();
  });

  it('keeps the durable opt-out when a later cleanup-state write fails', async () => {
    const disabled = disabledIntentStore();
    disabled.setCleanupStatus = vi.fn(() => {
      throw new Error('storage blocked');
    });
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') return null;
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'failed',
      remoteRevoked: false,
      nativeRevoked: 'failed',
    });
    await expect(reconcileWebPush(api, pendingRegistrationStore(), disabled))
      .resolves.toMatchObject({ status: 'disable_pending' });

    expect(disabled.value).toBe(true);
    expect(disabled.cleanup).toBe('pending');
    expect(api.unsubscribeWebPush).not.toHaveBeenCalled();
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it('persists cleanup state within one account-origin store and isolates another', () => {
    const accountA = memoryStorage();
    const accountB = memoryStorage();
    const firstPage = createNativePushDisabledIntentStore(accountA.storage);
    const otherAccount = createNativePushDisabledIntentStore(accountB.storage);

    firstPage.disable();
    firstPage.setCleanupStatus('failed');

    const reloadedPage = createNativePushDisabledIntentStore(accountA.storage);
    expect(reloadedPage.isDisabled()).toBe(true);
    expect(reloadedPage.cleanupStatus()).toBe('failed');
    expect(otherAccount.isDisabled()).toBe(false);
    expect(otherAccount.cleanupStatus()).toBeNull();

    reloadedPage.clear();
    expect(firstPage.isDisabled()).toBe(false);
  });

  it('reports a persisted native opt-out as off without reading native state', async () => {
    const localStorage = memoryStorage();
    createNativePushDisabledIntentStore(localStorage.storage).disable();
    const call = vi.fn();
    vi.stubGlobal('window', {
      avibeNative: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'test',
        capabilities: { push: 1 },
        call,
        on: vi.fn(() => () => undefined),
      },
      localStorage: localStorage.storage,
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(),
    } as unknown as ApiContextType;

    await expect(readWebPushDeviceState(api)).resolves.toBe('off');
    expect(call).not.toHaveBeenCalled();
    expect(api.getWebPushVapidPublicKey).not.toHaveBeenCalled();
  });

  it('serializes disable behind an in-flight native reconciliation', async () => {
    const disabled = disabledIntentStore();
    let releasePost!: (value: { ok: boolean; accepted: boolean }) => void;
    const postComplete = new Promise<{ ok: boolean; accepted: boolean }>((resolve) => {
      releasePost = resolve;
    });
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          endpoint: 'https://avibe.bot/api/push/apns/native-old',
          keys: { p256dh: 'key', auth: 'auth' },
        };
      }
      if (method === 'push.unsubscribe') return { revoked: true };
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn(async () => ({
        current_subscription_enabled: false,
        current_subscription_repairable: false,
      })),
      subscribeWebPush: vi.fn(async () => postComplete),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
    } as unknown as ApiContextType;

    const reconciliation = reconcileWebPush(api, pendingRegistrationStore(), disabled);
    await vi.waitFor(() => expect(api.subscribeWebPush).toHaveBeenCalledOnce());
    const disabling = disableWebPush(api, disabled);
    let disableSettled = false;
    void disabling.then(() => { disableSettled = true; });
    await Promise.resolve();
    expect(disableSettled).toBe(false);

    releasePost({ ok: true, accepted: true });
    await expect(reconciliation).resolves.toMatchObject({ status: 'enabled' });
    await expect(disabling).resolves.toMatchObject({ status: 'disabled' });
    expect(disabled.value).toBe(true);
    expect(api.subscribeWebPush).toHaveBeenCalledOnce();
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
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

  it('subscribes once when the user explicitly enables native push', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') return { state: 'off' as const };
      if (method === 'push.subscribe') {
        return {
          endpoint: 'https://avibe.bot/api/push/apns/native-new',
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
    } as unknown as ApiContextType;

    await expect(enableWebPush(api)).resolves.toEqual({
      endpoint: 'https://avibe.bot/api/push/apns/native-new',
      keys: { p256dh: 'new-key', auth: 'new-auth' },
    });

    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'push.status',
      'push.subscribe',
    ]);
    expect(api.subscribeWebPush).toHaveBeenCalledOnce();
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
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

    await expect(reconcileWebPush(api, pendingRegistrationStore())).resolves.toMatchObject({
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

  it('clears a rejected native recovery after unsubscribing it', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          endpoint: 'https://avibe.bot/api/push/apns/native-old',
          keys: { p256dh: 'old-key', auth: 'old-auth' },
        };
      }
      if (method === 'push.subscribe') {
        return {
          endpoint: 'https://avibe.bot/api/push/apns/native-new',
          keys: { p256dh: 'new-key', auth: 'new-auth' },
        };
      }
      if (method === 'push.unsubscribe') return { revoked: true };
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn(async () => ({
        current_subscription_enabled: false,
        current_subscription_repairable: true,
      })),
      subscribeWebPush: vi.fn(async () => ({
        ok: true,
        accepted: false,
        subscription: null,
      })),
    } as unknown as ApiContextType;
    const store = pendingRegistrationStore();

    await expect(reconcileWebPush(api, store)).resolves.toMatchObject({
      status: 'disabled',
    });

    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'push.status',
      'push.subscribe',
      'push.unsubscribe',
    ]);
    expect(store.value).toBeNull();
  });

  it('does not prompt for native permission when status is off without a remembered endpoint', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') return { state: 'off' as const };
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
      localStorage: { getItem: () => null },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn(),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(
      reconcileWebPush(api, pendingRegistrationStore()),
    ).resolves.toMatchObject({ status: 'disabled' });

    expect(call.mock.calls.map(([method]) => method)).toEqual(['push.status']);
    expect(api.getWebPushStatus).not.toHaveBeenCalled();
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it.each([
    ['enabled', true, false],
    ['repairable', false, true],
  ])(
    'subscribes and posts when the remembered native endpoint is %s',
    async (_label, currentEnabled, currentRepairable) => {
      const call = vi.fn(async (method: string) => {
        if (method === 'push.status') return { state: 'off' as const };
        if (method === 'push.subscribe') {
          return {
            endpoint: 'https://avibe.bot/api/push/apns/native-new',
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
        localStorage: {
          getItem: () => JSON.stringify([
            'https://avibe.bot/api/push/apns/native-old',
          ]),
          setItem: vi.fn(),
        },
      });
      const api = {
        getWebPushVapidPublicKey: vi.fn(async () => ({
          public_key: 'AQIDBA',
        })),
        getWebPushStatus: vi.fn(async () => ({
          current_subscription_enabled: currentEnabled,
          current_subscription_repairable: currentRepairable,
        })),
        subscribeWebPush: vi.fn(async () => ({
          ok: true,
          accepted: true,
        })),
      } as unknown as ApiContextType;
      const store = pendingRegistrationStore();

      await expect(reconcileWebPush(api, store)).resolves.toMatchObject({
        status: 'enabled',
      });

      expect(call.mock.calls.map(([method]) => method)).toEqual([
        'push.status',
        'push.subscribe',
      ]);
      expect(api.getWebPushStatus).toHaveBeenCalledWith({
        endpoint: 'https://avibe.bot/api/push/apns/native-old',
      });
      expect(api.subscribeWebPush).toHaveBeenCalledWith(
        {
          endpoint: 'https://avibe.bot/api/push/apns/native-new',
          keys: { p256dh: 'new-key', auth: 'new-auth' },
        },
        'iPhone App',
        undefined,
        ['https://avibe.bot/api/push/apns/native-old'],
        undefined,
      );
      expect(store.value).toBeNull();
    },
  );

  it('does not subscribe when the remembered native endpoint status fails', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') return { state: 'off' as const };
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
      localStorage: {
        getItem: () => JSON.stringify([
          'https://avibe.bot/api/push/apns/native-old',
        ]),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn(async () => {
        throw new Error('offline');
      }),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(
      reconcileWebPush(api, pendingRegistrationStore()),
    ).resolves.toMatchObject({ status: 'disabled' });

    expect(call.mock.calls.map(([method]) => method)).toEqual(['push.status']);
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it('retries an interrupted native POST without creating another subscription', async () => {
    let statusRead = 0;
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        statusRead += 1;
        return statusRead === 1
          ? { state: 'off' as const }
          : {
              state: 'on' as const,
              endpoint: 'https://avibe.bot/api/push/apns/native-new',
              keys: { p256dh: 'new-key', auth: 'new-auth' },
            };
      }
      if (method === 'push.subscribe') {
        return {
          endpoint: 'https://avibe.bot/api/push/apns/native-new',
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
      localStorage: {
        getItem: () => JSON.stringify([
          'https://avibe.bot/api/push/apns/native-old',
        ]),
        setItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn(async () => ({
        current_subscription_enabled: true,
        current_subscription_repairable: false,
      })),
      subscribeWebPush: vi.fn()
        .mockRejectedValueOnce(new Error('offline'))
        .mockResolvedValueOnce({ ok: true, accepted: true }),
    } as unknown as ApiContextType;
    const store = pendingRegistrationStore();

    await expect(reconcileWebPush(api, store)).resolves.toMatchObject({
      status: 'disabled',
    });
    expect(store.value?.endpoint).toBe(
      'https://avibe.bot/api/push/apns/native-new',
    );

    await expect(reconcileWebPush(api, store)).resolves.toMatchObject({
      status: 'enabled',
    });
    expect(call.mock.calls.filter(([method]) => method === 'push.subscribe')).toHaveLength(1);
    expect(api.subscribeWebPush).toHaveBeenCalledTimes(2);
    expect(store.value).toBeNull();
  });

  it('posts the current native subscription when the instance has no row', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          endpoint: 'https://avibe.bot/api/push/apns/native-current',
          keys: { p256dh: 'key', auth: 'auth' },
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
      localStorage: { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn(async () => ({
        current_subscription_enabled: false,
        current_subscription_repairable: false,
      })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
    } as unknown as ApiContextType;

    await expect(
      reconcileWebPush(api, pendingRegistrationStore()),
    ).resolves.toMatchObject({ status: 'enabled' });

    expect(call.mock.calls.map(([method]) => method)).toEqual(['push.status']);
    expect(api.subscribeWebPush).toHaveBeenCalledWith(
      {
        endpoint: 'https://avibe.bot/api/push/apns/native-current',
        keys: { p256dh: 'key', auth: 'auth' },
      },
      'iPhone App',
      undefined,
      ['https://avibe.bot/api/push/apns/native-current'],
      undefined,
    );
  });

  it('does not subscribe when native push is unavailable', async () => {
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') return { state: 'unavailable' as const };
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
      localStorage: {
        getItem: () => null,
        setItem: vi.fn(),
        removeItem: vi.fn(),
      },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(
      reconcileWebPush(api, pendingRegistrationStore()),
    ).resolves.toMatchObject({ status: 'disabled' });
    expect(call).toHaveBeenCalledOnce();
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it('shares one in-flight reconciliation between load and foreground', async () => {
    let releaseKey: (() => void) | undefined;
    const keyReady = new Promise<void>((resolve) => {
      releaseKey = resolve;
    });
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return { state: 'unavailable' as const };
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
      localStorage: { getItem: () => null, setItem: vi.fn(), removeItem: vi.fn() },
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => {
        await keyReady;
        return { public_key: 'AQIDBA' };
      }),
    } as unknown as ApiContextType;

    const store = pendingRegistrationStore();
    const first = reconcileWebPush(api, store);
    const second = reconcileWebPush(api, store);

    expect(second).toBe(first);
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
    expect(api.getWebPushVapidPublicKey).toHaveBeenCalledOnce();
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
