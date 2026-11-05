/** @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  hasNativeCapability,
  isNativeShell,
  nativeAppInfo,
  nativeHapticImpact,
  nativeVoicePermission,
  openNativeExternal,
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
    await expect(nativeHapticImpact('medium')).resolves.toBe(true);
    await expect(setNativeOuterScrollLocked(true)).resolves.toBe(true);
    await expect(nativeVoicePermission()).resolves.toBe('granted');
    expect(call.mock.calls.map(([method]) => method)).toEqual([
      'app.info',
      'app.openExternal',
      'haptics.impact',
      'viewport.setOuterScrollLocked',
      'voice.permission',
    ]);
  });

  it('uses harmless fallbacks when a capability is missing or a call fails', async () => {
    installBridge({
      capabilities: { app: 1 },
      call: vi.fn(async () => {
        throw { code: 'failed', message: 'unavailable' };
      }),
    });

    await expect(nativeHapticImpact('light')).resolves.toBe(false);
    await expect(setNativeOuterScrollLocked(true)).resolves.toBe(false);
    await expect(nativeVoicePermission()).resolves.toBeNull();
    await expect(openNativeExternal('https://avibe.bot')).resolves.toBe(false);
  });
});
