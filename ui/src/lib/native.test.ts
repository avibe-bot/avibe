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
  setNativeAccountActionAvailable,
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
