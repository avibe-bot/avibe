/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  hasNativeCapability,
  isNativeShell,
  nativeAppInfo,
  nativeHapticImpact,
  nativePushStatusOutcome,
  nativePushSubscribe,
  nativePushUnsubscribeOutcome,
  nativeShowAccount,
  nativeVoicePermission,
  onNativePushOpened,
  openNativeExternal,
  NATIVE_RECONCILE_MAX_SESSIONS,
  reconcileNativeNotifications,
  setNativeAccountActionAvailable,
  setNativeBadge,
  setNativeOuterScrollLocked,
} from './native';

const installBridge = (
  overrides: Partial<NonNullable<Window['avibeNative']>> = {},
) => {
  const call = vi.fn(async (method: string) => {
    if (method === 'app.info') {
      return {
        contract: 1,
        platform: 'ios',
        shellVersion: '1.0 (1)',
        capabilities: {
          accountPresentation: 2,
          app: 1,
          haptics: 1,
          keyboardResizesViewport: 1,
          viewport: 1,
          voice: 1,
        },
      };
    }
    if (method === 'app.openExternal') return { opened: true };
    if (method === 'voice.permission') return { state: 'granted' };
    return null;
  });
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities: {
        accountPresentation: 2,
        app: 1,
        haptics: 1,
        keyboardResizesViewport: 1,
        viewport: 1,
        voice: 1,
      },
      call,
      on: vi.fn(() => () => undefined),
      ...overrides,
    },
  });
  return call;
};

afterEach(() => {
  Reflect.deleteProperty(window, 'avibeNative');
  vi.restoreAllMocks();
});

describe('native shell bridge', () => {
  it('accepts only the versioned native contract', () => {
    expect(isNativeShell()).toBe(false);
    installBridge();
    expect(isNativeShell()).toBe(true);
    expect(hasNativeCapability('accountPresentation')).toBe(true);
    expect(hasNativeCapability('haptics')).toBe(true);
    expect(hasNativeCapability('keyboardResizesViewport')).toBe(true);
    expect(hasNativeCapability('haptics', 2)).toBe(false);

    installBridge({ contract: 2 as 1 });
    expect(isNativeShell()).toBe(false);
  });

  it('keeps all bridge calls behind typed helpers', async () => {
    const call = installBridge();

    await expect(nativeAppInfo()).resolves.toMatchObject({
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
    });
    await expect(openNativeExternal('https://avibe.bot')).resolves.toBe(true);
    await expect(nativeShowAccount()).resolves.toBe(true);
    await expect(setNativeAccountActionAvailable(true)).resolves.toBe(true);
    await expect(nativeHapticImpact('medium')).resolves.toBe(true);
    await expect(setNativeOuterScrollLocked(true)).resolves.toBe(true);
    await expect(nativeVoicePermission()).resolves.toBe('granted');
    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'app.info',
      'app.openExternal',
      'app.showAccount',
      'app.setAccountActionAvailable',
      'haptics.impact',
      'viewport.setOuterScrollLocked',
      'voice.permission',
    ]);
  });

  it('uses harmless fallbacks when a capability is missing or a call fails', async () => {
    const missingCall = installBridge({
      capabilities: { app: 1 },
    });
    await expect(nativeShowAccount()).resolves.toBe(false);
    await expect(setNativeAccountActionAvailable(true)).resolves.toBe(false);
    expect(missingCall).not.toHaveBeenCalledWith('app.showAccount', {});

    installBridge({
      capabilities: { accountPresentation: 1, app: 1 },
      call: vi.fn(async () => {
        throw { code: 'failed', message: 'unavailable' };
      }),
    });

    await expect(nativeHapticImpact('light')).resolves.toBe(false);
    await expect(setNativeOuterScrollLocked(true)).resolves.toBe(false);
    await expect(nativeVoicePermission()).resolves.toBeNull();
    await expect(openNativeExternal('https://avibe.bot')).resolves.toBe(false);
    await expect(nativeShowAccount()).resolves.toBe(false);
    await expect(setNativeAccountActionAvailable(true)).resolves.toBe(false);
  });

  it('sets the App badge to the count it is given, 0 included, and only where the shell has the badge capability', async () => {
    const call = installBridge({ capabilities: { app: 1, badge: 1 } });

    expect(setNativeBadge(3)).toBe(true);
    expect(setNativeBadge(0)).toBe(true);
    // A count the bridge would refuse is sent as the count it stands for.
    expect(setNativeBadge(2.7)).toBe(true);
    expect(setNativeBadge(-1)).toBe(true);
    expect(call.mock.calls).toEqual([
      ['badge.set', { count: 3 }],
      ['badge.set', { count: 0 }],
      ['badge.set', { count: 2 }],
      ['badge.set', { count: 0 }],
    ]);

    // A failing call is the shell's business, never the page's.
    installBridge({
      capabilities: { badge: 1 },
      call: vi.fn(async () => {
        throw { code: 'forbidden', message: 'not the selected instance' };
      }),
    });
    expect(setNativeBadge(1)).toBe(true);
    await Promise.resolve();

    // An older shell, and a plain browser, have no native badge.
    const older = installBridge({ capabilities: { app: 1, push: 1 } });
    expect(setNativeBadge(3)).toBe(false);
    expect(older).not.toHaveBeenCalled();
    Reflect.deleteProperty(window, 'avibeNative');
    expect(setNativeBadge(3)).toBe(false);
  });

  it('names the unread Sessions of a whole map to the App, or nothing when it cannot name them all', () => {
    const call = installBridge({ capabilities: { app: 1, notifications: 1 } });

    // Only counts above zero are unread.
    expect(reconcileNativeNotifications({ ses_a: 2, ses_b: 0, ses_c: 1 }, 1_000)).toBe(true);
    expect(reconcileNativeNotifications({}, 2_000)).toBe(true);
    expect(call.mock.calls).toEqual([
      ['notifications.reconcile', { unreadSessions: ['ses_a', 'ses_c'], asOf: 1_000 }],
      ['notifications.reconcile', { unreadSessions: [], asOf: 2_000 }],
    ]);

    // A Session the call could not name would read as read: nothing is sent.
    const tooMany = Object.fromEntries(
      Array.from({ length: NATIVE_RECONCILE_MAX_SESSIONS + 1 }, (_, i) => [`ses_${i}`, 1]),
    );
    const atLimit = Object.fromEntries(Object.entries(tooMany).slice(0, NATIVE_RECONCILE_MAX_SESSIONS));
    for (const unread of [
      tooMany,
      { ['s'.repeat(201)]: 1 },
      { ses_a: Number.NaN },
      { ses_a: '1' },
      null,
      undefined,
      ['ses_a'],
    ]) {
      expect(reconcileNativeNotifications(unread, 3_000)).toBe(false);
    }
    expect(reconcileNativeNotifications({ ses_a: 1 }, Number.NaN)).toBe(false);
    expect(call).toHaveBeenCalledTimes(2);
    expect(reconcileNativeNotifications(atLimit, 4_000)).toBe(true);
    expect(call).toHaveBeenCalledTimes(3);

    // An older shell, and a plain browser, have no native reconcile.
    const older = installBridge({ capabilities: { app: 1, badge: 1 } });
    expect(reconcileNativeNotifications({ ses_a: 1 }, 1_000)).toBe(false);
    expect(older).not.toHaveBeenCalled();
    Reflect.deleteProperty(window, 'avibeNative');
    expect(reconcileNativeNotifications({ ses_a: 1 }, 1_000)).toBe(false);
  });

  it.each([
    ['legacy confirmed', { revoked: true }, 'confirmed'],
    ['legacy deferred', { revoked: false }, 'pending'],
    ['confirmed', { revoked: true, revoke: 'confirmed' }, 'confirmed'],
    ['deferred', { revoked: false, revoke: 'pending' }, 'pending'],
    ['absent', { revoked: true, revoke: 'not_needed' }, 'not_needed'],
  ])(
    'keeps the %s native push revoke outcome distinct',
    async (_label, result, expected) => {
      installBridge({
        capabilities: { push: 1 },
        call: vi.fn(async () => result),
      });

      await expect(nativePushUnsubscribeOutcome()).resolves.toEqual({
        status: expected,
      });
    },
  );

  it('reports a native push bridge failure separately from deferred cleanup', async () => {
    installBridge({
      capabilities: { push: 1 },
      call: vi.fn(async () => {
        throw new Error('bridge unavailable');
      }),
    });

    await expect(nativePushUnsubscribeOutcome()).resolves.toEqual({
      status: 'failed',
    });
    await expect(nativePushStatusOutcome('AQIDBA')).resolves.toEqual({
      status: 'failed',
    });
  });

  it('preserves pending-revoke status facts from the native bridge', async () => {
    installBridge({
      capabilities: { push: 1 },
      call: vi.fn(async () => ({
        state: 'off',
        revokeState: 'pending',
        endpoint: 'https://avibe.bot/api/push/apns/pending',
      })),
    });

    await expect(nativePushStatusOutcome('AQIDBA')).resolves.toEqual({
      status: 'ok',
      value: {
        state: 'off',
        revokeState: 'pending',
        endpoint: 'https://avibe.bot/api/push/apns/pending',
      },
    });
  });

  it('sends force replacement as an additive subscribe option', async () => {
    const call = vi.fn(async (_method: string, params?: Record<string, unknown>) => ({
      endpoint: 'https://avibe.bot/api/push/apns/registration',
      keys: { p256dh: 'key', auth: 'auth' },
      params,
    }));
    installBridge({
      capabilities: { push: 1 },
      call,
    });

    await expect(nativePushSubscribe('AQIDBA')).resolves.toMatchObject({
      endpoint: 'https://avibe.bot/api/push/apns/registration',
    });
    await expect(nativePushSubscribe('AQIDBA', true)).resolves.toMatchObject({
      endpoint: 'https://avibe.bot/api/push/apns/registration',
    });

    expect(call.mock.calls).toEqual([
      [
        'push.subscribe',
        { vapidPublicKey: 'AQIDBA' },
      ],
      [
        'push.subscribe',
        { vapidPublicKey: 'AQIDBA', forceResubscribe: true },
      ],
    ]);
  });

  it('normalizes same-origin native notification paths and rejects foreign origins', () => {
    const on = vi.fn((event: string, handler: (payload: unknown) => void) => {
      if (event === 'push.opened') {
        handler({ url: '/chat/ses_1?from=push#latest' });
        handler({ url: 'https://avibe.bot/chat/ses_2' });
        handler({ url: '//evil.example/phish' });
        handler({ url: '/\\evil.example/phish' });
      }
      return () => undefined;
    });
    installBridge({
      capabilities: { push: 1 },
      on,
    });

    const handler = vi.fn();
    onNativePushOpened(handler);

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith('/chat/ses_1?from=push#latest');
  });
});
