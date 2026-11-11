import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ApiContextType } from '@/context/ApiContext';
import {
  createNativePushDisabledIntentStore,
  disableWebPush,
  enableWebPush,
  type NativePushDisableRecord,
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
  value: NativePushDisableRecord | null;
} {
  let revision = initial ? 1 : 0;
  const initialRecord = (): NativePushDisableRecord => ({
    version: 3,
    intentRevision: revision,
    phase: 'cleanup',
    userIntent: 'disabled',
    endpoint: null,
    instanceCleanup: 'pending',
    nativeRevoke: 'pending',
  });
  return {
    value: initial ? initialRecord() : null,
    isDisabled() {
      return this.value !== null;
    },
    read() {
      return this.value;
    },
    readIntent() {
      return {
        revision,
        userIntent: this.value ? 'disabled' : 'enabled',
      };
    },
    isCurrentIntentRevision(currentRevision) {
      return currentRevision === revision;
    },
    beginDisable() {
      if (this.value) {
        return {
          record: this.value,
          created: false,
          revision,
        };
      }
      revision += 1;
      this.value = {
        version: 3,
        intentRevision: revision,
        phase: 'capture_pending',
        userIntent: 'disabled',
        endpoint: null,
        instanceCleanup: 'unknown',
        nativeRevoke: 'unknown',
      };
      return { record: this.value, created: true, revision };
    },
    beginEnable() {
      revision += 1;
      this.value = null;
      return { revision };
    },
    update(patch) {
      if (!this.value) throw new Error('missing disable record');
      if (
        this.value.endpoint
        && patch.endpoint !== undefined
        && patch.endpoint !== this.value.endpoint
      ) {
        throw new Error('endpoint changed');
      }
      this.value = {
        ...this.value,
        ...patch,
        intentRevision: revision,
        phase: patch.phase
          ?? (patch.endpoint && this.value.phase === 'capture_pending'
            ? 'cleanup'
            : this.value.phase),
        endpoint: this.value.endpoint ?? patch.endpoint ?? null,
      };
      return this.value;
    },
    clear() {
      this.beginEnable();
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

function writableLocalStorage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
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
      .toEqual({
        endpoint: 'https://avibe.bot/api/push/apns/native-old',
        intentRevision: 2,
        instanceCleanup: 'confirmed',
        nativeRevoke: 'confirmed',
        phase: 'cleanup',
        userIntent: 'disabled',
        version: 3,
      });
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
      localStorage: writableLocalStorage(),
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

  it('advances instance and native cleanup independently without false confirmation', async () => {
    const disabled = disabledIntentStore();
    let nativeUnsubscribeCount = 0;
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          revokeState: 'active' as const,
          endpoint: 'https://avibe.bot/api/push/apns/native-old',
          keys: { p256dh: 'key', auth: 'auth' },
        };
      }
      if (method === 'push.unsubscribe') {
        nativeUnsubscribeCount += 1;
        return nativeUnsubscribeCount < 3
          ? { revoked: false, revoke: 'pending' }
          : { revoked: true, revoke: 'confirmed' };
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
      localStorage: writableLocalStorage(),
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn()
        .mockRejectedValueOnce(new Error('offline'))
        .mockResolvedValue({ ok: true, disabled: true }),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'failed',
      remoteRevoked: false,
      instanceCleanup: 'failed',
      nativeRevoked: 'pending',
    });
    await expect(reconcileWebPush(api, pendingRegistrationStore(), disabled))
      .resolves.toMatchObject({ status: 'disable_failed' });

    expect(disabled.isDisabled()).toBe(true);
    expect(api.subscribeWebPush).not.toHaveBeenCalled();

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'pending',
      remoteRevoked: true,
      instanceCleanup: 'confirmed',
      nativeRevoked: 'pending',
    });
    expect(disabled.read()).toMatchObject({
      instanceCleanup: 'confirmed',
      nativeRevoke: 'pending',
    });

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'disabled',
      remoteRevoked: true,
      instanceCleanup: 'confirmed',
      nativeRevoked: 'confirmed',
    });
    expect(api.unsubscribeWebPush).toHaveBeenCalledTimes(2);
    expect(call).toHaveBeenCalledTimes(4);
    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'push.status',
      'push.unsubscribe',
      'push.unsubscribe',
      'push.unsubscribe',
    ]);
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
      localStorage: writableLocalStorage(),
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

    expect(disabled.isDisabled()).toBe(true);
    expect(disabled.read()).toMatchObject({
      instanceCleanup: 'confirmed',
      nativeRevoke: 'failed',
    });
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
      localStorage: writableLocalStorage(),
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

    expect(disabled.read()).toMatchObject({
      instanceCleanup: 'failed',
      nativeRevoke: 'confirmed',
    });
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it('retries the captured instance endpoint after native unsubscribe leaves state off', async () => {
    const localStorage = memoryStorage();
    let nativeEnabled = true;
    const endpoint = 'https://avibe.bot/api/push/apns/native-old';
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return nativeEnabled
          ? {
              state: 'on' as const,
              endpoint,
              keys: { p256dh: 'key', auth: 'auth' },
            }
          : { state: 'off' as const };
      }
      if (method === 'push.unsubscribe') {
        nativeEnabled = false;
        return { revoked: true };
      }
      if (method === 'push.subscribe') {
        throw new Error('native subscribe must not run during cleanup retry');
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
      localStorage: localStorage.storage,
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn()
        .mockRejectedValueOnce(new Error('offline'))
        .mockResolvedValueOnce({ ok: true, disabled: true }),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    const first = await disableWebPush(api);
    const second = await disableWebPush(api);

    expect(first).toMatchObject({
      status: 'failed',
      remoteRevoked: false,
      nativeRevoked: 'confirmed',
    });
    expect(second).toMatchObject({
      status: 'disabled',
      remoteRevoked: true,
      nativeRevoked: 'confirmed',
    });
    expect(api.unsubscribeWebPush).toHaveBeenNthCalledWith(1, endpoint);
    expect(api.unsubscribeWebPush).toHaveBeenNthCalledWith(2, endpoint);
    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'push.status',
      'push.unsubscribe',
    ]);
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
    expect(JSON.parse(localStorage.values.get('vibe.nativePush.disabledIntent') ?? '{}'))
      .toEqual({
        endpoint,
        intentRevision: 1,
        instanceCleanup: 'confirmed',
        nativeRevoke: 'confirmed',
        phase: 'cleanup',
        userIntent: 'disabled',
        version: 3,
      });
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
      localStorage: writableLocalStorage(),
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
    } as unknown as ApiContextType;

    await expect(enableWebPush(api, { disabledIntentStore: disabled })).resolves.toMatchObject({
      endpoint: 'https://avibe.bot/api/push/apns/native-new',
    });

    expect(disabled.isDisabled()).toBe(false);
    expect(api.subscribeWebPush).toHaveBeenCalledOnce();
  });

  it('does not acknowledge a native disable when its intent cannot be persisted', async () => {
    const disabled: NativePushDisabledIntentStore = {
      isDisabled: () => false,
      read: () => null,
      readIntent: () => ({ revision: 0, userIntent: 'enabled' }),
      isCurrentIntentRevision: () => true,
      beginDisable: () => { throw new Error('storage blocked'); },
      beginEnable: () => ({ revision: 1 }),
      update: vi.fn(),
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
      localStorage: writableLocalStorage(),
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(),
      unsubscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).rejects.toThrow('storage blocked');
    expect(call).not.toHaveBeenCalled();
    expect(api.unsubscribeWebPush).not.toHaveBeenCalled();
  });

  it('does not start cleanup when the captured endpoint cannot be persisted', async () => {
    const disabled = disabledIntentStore();
    const update = disabled.update.bind(disabled);
    disabled.update = (patch) => {
      if (patch.endpoint) throw new Error('storage blocked');
      return update(patch);
    };
    const endpoint = 'https://avibe.bot/api/push/apns/native-old';
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          endpoint,
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
      localStorage: writableLocalStorage(),
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'failed',
      remoteRevoked: false,
      nativeRevoked: 'unknown',
    });
    expect(call).toHaveBeenCalledOnce();
    expect(api.unsubscribeWebPush).not.toHaveBeenCalled();
  });

  it('keeps the durable opt-out when a later cleanup-state write fails', async () => {
    const disabled = disabledIntentStore();
    disabled.update = vi.fn(() => {
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
      localStorage: writableLocalStorage(),
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'failed',
      remoteRevoked: false,
      nativeRevoked: 'unknown',
    });
    await expect(reconcileWebPush(api, pendingRegistrationStore(), disabled))
      .resolves.toMatchObject({ status: 'disable_failed' });

    expect(disabled.isDisabled()).toBe(true);
    expect(disabled.read()).toMatchObject({
      instanceCleanup: 'unknown',
      nativeRevoke: 'unknown',
    });
    expect(api.unsubscribeWebPush).not.toHaveBeenCalled();
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it('persists cleanup state within one account-origin store and isolates another', () => {
    const accountA = memoryStorage();
    const accountB = memoryStorage();
    const firstPage = createNativePushDisabledIntentStore(accountA.storage);
    const otherAccount = createNativePushDisabledIntentStore(accountB.storage);

    firstPage.beginDisable();
    firstPage.update({
      endpoint: 'https://push.example.test/sub/old',
      instanceCleanup: 'failed',
      nativeRevoke: 'pending',
    });

    const reloadedPage = createNativePushDisabledIntentStore(accountA.storage);
    expect(reloadedPage.isDisabled()).toBe(true);
    expect(reloadedPage.read()).toEqual({
      version: 3,
      intentRevision: 1,
      phase: 'cleanup',
      userIntent: 'disabled',
      endpoint: 'https://push.example.test/sub/old',
      instanceCleanup: 'failed',
      nativeRevoke: 'pending',
    });
    expect(otherAccount.isDisabled()).toBe(false);
    expect(otherAccount.read()).toBeNull();

    reloadedPage.clear();
    expect(firstPage.isDisabled()).toBe(false);
    expect(firstPage.read()).toBeNull();
  });

  it('fails closed for an old opt-out marker without an endpoint even when native is on', async () => {
    const localStorage = memoryStorage();
    localStorage.storage.setItem(
      'vibe.nativePush.disabledIntent',
      JSON.stringify({ cleanup: 'failed' }),
    );
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          revokeState: 'active' as const,
          endpoint: 'https://avibe.bot/api/push/apns/current',
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
      localStorage: localStorage.storage,
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      unsubscribeWebPush: vi.fn(),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api)).resolves.toMatchObject({
      status: 'failed',
      remoteRevoked: false,
      nativeRevoked: 'unknown',
    });

    expect(api.unsubscribeWebPush).not.toHaveBeenCalled();
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
    expect(call).not.toHaveBeenCalled();
  });

  it('fails closed when native intent storage cannot be read', async () => {
    const blockedStorage = {
      getItem: () => { throw new Error('storage blocked'); },
      setItem: vi.fn(),
      removeItem: vi.fn(),
    };
    const call = vi.fn();
    const disabled = createNativePushDisabledIntentStore(blockedStorage);
    vi.stubGlobal('window', {
      avibeNative: {
        contract: 1,
        platform: 'ios',
        shellVersion: 'test',
        capabilities: { push: 1 },
        call,
        on: vi.fn(() => () => undefined),
      },
      localStorage: blockedStorage,
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(),
      subscribeWebPush: vi.fn(),
      unsubscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(
      reconcileWebPush(api, pendingRegistrationStore(), disabled),
    ).resolves.toMatchObject({ status: 'disable_failed' });
    expect(call).not.toHaveBeenCalled();
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
  });

  it('reports a persisted native opt-out as off without reading native state', async () => {
    const localStorage = memoryStorage();
    createNativePushDisabledIntentStore(localStorage.storage).beginDisable();
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

  it('blocks an in-flight reconciliation from posting after a newer disable', async () => {
    const disabled = disabledIntentStore();
    let releaseStatus!: () => void;
    const statusReady = new Promise<void>((resolve) => {
      releaseStatus = resolve;
    });
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          revokeState: 'active' as const,
          endpoint: 'https://avibe.bot/api/push/apns/native-old',
          keys: { p256dh: 'key', auth: 'auth' },
        };
      }
      if (method === 'push.unsubscribe') {
        return { revoked: true, revoke: 'confirmed' };
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
      localStorage: memoryStorage().storage,
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn(async () => {
        await statusReady;
        return {
          current_subscription_enabled: false,
          current_subscription_repairable: false,
        };
      }),
      subscribeWebPush: vi.fn(),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
    } as unknown as ApiContextType;

    const reconciliation = reconcileWebPush(api, pendingRegistrationStore(), disabled);
    await vi.waitFor(() => expect(api.getWebPushStatus).toHaveBeenCalledOnce());
    const disabling = disableWebPush(api, disabled);
    await vi.waitFor(() => expect(disabled.isDisabled()).toBe(true));

    releaseStatus();
    await expect(reconciliation).resolves.toMatchObject({ status: 'disabled' });
    await expect(disabling).resolves.toMatchObject({ status: 'disabled' });
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
    expect(api.unsubscribeWebPush).toHaveBeenCalledOnce();
  });

  it('lets a newer same-origin disable supersede an earlier enable before subscribe', async () => {
    const sharedStorage = memoryStorage();
    const firstDocument = createNativePushDisabledIntentStore(sharedStorage.storage);
    const secondDocument = createNativePushDisabledIntentStore(sharedStorage.storage);
    let releaseVapid!: () => void;
    const vapidReady = new Promise<void>((resolve) => {
      releaseVapid = resolve;
    });
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return { state: 'off' as const, revokeState: 'absent' as const };
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
      localStorage: sharedStorage.storage,
    });
    let vapidCalls = 0;
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => {
        vapidCalls += 1;
        if (vapidCalls === 1) await vapidReady;
        return { public_key: 'AQIDBA' };
      }),
      subscribeWebPush: vi.fn(),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
    } as unknown as ApiContextType;

    const enable = enableWebPush(api, {
      disabledIntentStore: firstDocument,
    });
    await vi.waitFor(() => expect(api.getWebPushVapidPublicKey).toHaveBeenCalledOnce());

    const disable = disableWebPush(api, secondDocument);
    await vi.waitFor(() => expect(secondDocument.isDisabled()).toBe(true));
    releaseVapid();

    await expect(enable).rejects.toThrow('native_push_stale_intent');
    await expect(disable).resolves.toMatchObject({ status: 'disabled' });
    expect(call).not.toHaveBeenCalledWith('push.subscribe', expect.anything());
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
    expect(secondDocument.readIntent()).toMatchObject({
      revision: 2,
      userIntent: 'disabled',
    });
  });

  it('compensates a stale native endpoint before posting it', async () => {
    const sharedStorage = memoryStorage();
    const firstDocument = createNativePushDisabledIntentStore(sharedStorage.storage);
    const secondDocument = createNativePushDisabledIntentStore(sharedStorage.storage);
    let releaseCache!: () => void;
    const cacheReady = new Promise<void>((resolve) => {
      releaseCache = resolve;
    });
    const cache = {
      match: vi.fn(async () => {
        await cacheReady;
        return undefined;
      }),
    };
    const endpoint = 'https://avibe.bot/api/push/apns/stale-enable';
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return { state: 'off' as const, revokeState: 'absent' as const };
      }
      if (method === 'push.subscribe') {
        return {
          endpoint,
          keys: { p256dh: 'key', auth: 'auth' },
        };
      }
      if (method === 'push.unsubscribe') {
        return { revoked: true, revoke: 'confirmed' };
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
      caches: {
        open: vi.fn(async () => cache),
      },
      location: { origin: 'https://avibe.bot' },
      localStorage: sharedStorage.storage,
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(async () => ({ ok: true, accepted: true })),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
    } as unknown as ApiContextType;

    const enable = enableWebPush(api, {
      disabledIntentStore: firstDocument,
    });
    await vi.waitFor(() => expect(call).toHaveBeenCalledWith(
      'push.subscribe',
      { vapidPublicKey: 'AQIDBA' },
    ));
    await vi.waitFor(() => expect(cache.match).toHaveBeenCalledOnce());

    const disable = disableWebPush(api, secondDocument);
    await vi.waitFor(() => expect(secondDocument.isDisabled()).toBe(true));
    releaseCache();

    await expect(enable).rejects.toThrow('native_push_stale_intent');
    await expect(disable).resolves.toMatchObject({ status: 'disabled' });
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
    expect(call.mock.calls.filter(([method]) => method === 'push.unsubscribe')).not.toHaveLength(0);
    expect(secondDocument.read()).toMatchObject({
      endpoint: null,
      instanceCleanup: 'confirmed',
      nativeRevoke: 'not_needed',
    });
  });

  it('resumes an interrupted first endpoint capture from capture_pending', async () => {
    const localStorage = memoryStorage();
    const disabled = createNativePushDisabledIntentStore(localStorage.storage);
    const endpoint = 'https://avibe.bot/api/push/apns/captured-on-retry';
    let nativeUnsubscribeCount = 0;
    const call = vi.fn(async (method: string) => {
      if (method === 'push.status') {
        return {
          state: 'on' as const,
          revokeState: 'active' as const,
          endpoint,
          keys: { p256dh: 'key', auth: 'auth' },
        };
      }
      if (method === 'push.unsubscribe') {
        nativeUnsubscribeCount += 1;
        return nativeUnsubscribeCount === 1
          ? { revoked: false, revoke: 'pending' as const }
          : { revoked: true, revoke: 'confirmed' as const };
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
      localStorage: localStorage.storage,
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn()
        .mockRejectedValueOnce(new Error('interrupted'))
        .mockResolvedValue({ public_key: 'AQIDBA' }),
      unsubscribeWebPush: vi.fn(async () => ({ ok: true, disabled: true })),
      subscribeWebPush: vi.fn(),
    } as unknown as ApiContextType;

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'failed',
    });
    expect(disabled.read()).toMatchObject({
      phase: 'capture_pending',
      endpoint: null,
    });

    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'pending',
      instanceCleanup: 'confirmed',
      nativeRevoked: 'pending',
    });
    await expect(disableWebPush(api, disabled)).resolves.toMatchObject({
      status: 'disabled',
      nativeRevoked: 'confirmed',
    });
    expect(api.unsubscribeWebPush).toHaveBeenCalledWith(endpoint);
    expect(api.subscribeWebPush).not.toHaveBeenCalled();
    expect(disabled.read()?.endpoint).toBe(endpoint);
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
      localStorage: writableLocalStorage(),
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
      localStorage: writableLocalStorage(),
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

  it('reports native push enabled only once the instance has accepted the subscription', async () => {
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
      localStorage: writableLocalStorage(),
    });
    let accept: ((value: { ok: true; accepted: true }) => void) | undefined;
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      subscribeWebPush: vi.fn(() => new Promise((resolve) => {
        accept = resolve;
      })),
    } as unknown as ApiContextType;
    const store = pendingRegistrationStore();

    let settled = false;
    const enabled = enableWebPush(api, { pendingRegistrationStore: store }).finally(() => {
      settled = true;
    });
    await vi.waitFor(() => expect(api.subscribeWebPush).toHaveBeenCalledOnce());
    // The native side is subscribed and the POST is out: still not enabled, and
    // the subscription waits in the pending store until the instance answers.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(settled).toBe(false);
    expect(store.value?.endpoint).toBe('https://avibe.bot/api/push/apns/native-new');

    accept?.({ ok: true, accepted: true });
    await expect(enabled).resolves.toMatchObject({
      endpoint: 'https://avibe.bot/api/push/apns/native-new',
    });
    expect(store.value).toBeNull();
  });

  it('reports a refused native registration as off, not registering', async () => {
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
    const refused = Object.assign(new Error('invalid subscription'), {
      status: 400,
      code: 'invalid_subscription',
    });
    const api = {
      getWebPushVapidPublicKey: vi.fn(async () => ({ public_key: 'AQIDBA' })),
      getWebPushStatus: vi.fn(async () => ({
        current_subscription_enabled: false,
        current_subscription_repairable: false,
      })),
      subscribeWebPush: vi.fn(async () => {
        throw refused;
      }),
    } as unknown as ApiContextType;
    const store = pendingRegistrationStore();

    // A retry cannot turn a refusal into a registration, so the control offers
    // to enable again instead of registering forever.
    await expect(reconcileWebPush(api, store)).resolves.toMatchObject({
      status: 'disabled',
    });
    expect(store.value?.endpoint).toBe('https://avibe.bot/api/push/apns/native-current');
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
      localStorage: writableLocalStorage(),
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
      localStorage: writableLocalStorage(),
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
          getItem: (key: string) => key === 'vibe.webPush.endpoints'
            ? JSON.stringify(['https://avibe.bot/api/push/apns/native-old'])
            : null,
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
        getItem: (key: string) => key === 'vibe.webPush.endpoints'
          ? JSON.stringify(['https://avibe.bot/api/push/apns/native-old'])
          : null,
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
        getItem: (key: string) => key === 'vibe.webPush.endpoints'
          ? JSON.stringify(['https://avibe.bot/api/push/apns/native-old'])
          : null,
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

    // The App holds the subscription and the instance has not stored it: not
    // enabled, and not off either; the next reconcile registers it.
    await expect(reconcileWebPush(api, store)).resolves.toMatchObject({
      status: 'registering',
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
