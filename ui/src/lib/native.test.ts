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
  onNativeDirectTransportChanged,
  onNativePushOpened,
  openNativeExternal,
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

  it('delivers each transport change once, in order, and only with the direct capability', () => {
    const on = vi.fn((event: string, deliver: (payload: unknown) => void) => {
      if (event === 'direct.transportChanged') {
        deliver({ transport: 'direct', sequence: 1 });
        // A repeat and an older change are both stale by the time they arrive.
        deliver({ transport: 'direct', sequence: 1 });
        deliver({ transport: 'tunnel', sequence: 0 });
        // Not the contract's shape: nothing to act on.
        deliver({ transport: 'relay', sequence: 2 });
        deliver({ transport: 'tunnel', sequence: '3' });
        deliver({ transport: 'tunnel' });
        deliver(null);
        deliver({ transport: 'tunnel', sequence: 4 });
      }
      return () => undefined;
    });

    installBridge({ on });
    const withoutCapability = vi.fn();
    onNativeDirectTransportChanged(withoutCapability);
    expect(on).not.toHaveBeenCalled();
    expect(withoutCapability).not.toHaveBeenCalled();

    installBridge({ capabilities: { direct: 1 }, on });
    const handler = vi.fn();
    onNativeDirectTransportChanged(handler);
    expect(handler.mock.calls).toEqual([
      [{ transport: 'direct', sequence: 1 }],
      [{ transport: 'tunnel', sequence: 4 }],
    ]);
  });
});
