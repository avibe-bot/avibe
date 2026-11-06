/** @vitest-environment jsdom */

import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useNativeAccountActionRegistration } from './useNativeAccountActionRegistration';

const installBridge = (accountPresentation = 2) => {
  const call = vi.fn(async () => null);
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities: { accountPresentation },
      call,
      on: vi.fn(() => () => undefined),
    },
  });
  return call;
};

afterEach(() => {
  Reflect.deleteProperty(window, 'avibeNative');
  vi.restoreAllMocks();
});

describe('useNativeAccountActionRegistration', () => {
  it('registers for the mounted AppShell lifetime and releases on unmount', () => {
    const call = installBridge();
    const hook = renderHook(() => useNativeAccountActionRegistration());

    expect(call).toHaveBeenCalledWith(
      'app.setAccountActionAvailable',
      { available: true },
    );

    hook.unmount();
    expect(call.mock.calls).toEqual([
      ['app.setAccountActionAvailable', { available: true }],
      ['app.setAccountActionAvailable', { available: false }],
    ]);
  });

  it('releases on pagehide and registers again after a page-cache restore', () => {
    const call = installBridge();
    const hook = renderHook(() => useNativeAccountActionRegistration());

    act(() => {
      window.dispatchEvent(new Event('pagehide'));
      window.dispatchEvent(new Event('pageshow'));
    });

    expect(call.mock.calls).toEqual([
      ['app.setAccountActionAvailable', { available: true }],
      ['app.setAccountActionAvailable', { available: false }],
      ['app.setAccountActionAvailable', { available: true }],
    ]);

    hook.unmount();
    expect(call).toHaveBeenLastCalledWith(
      'app.setAccountActionAvailable',
      { available: false },
    );
  });

  it('does not call a shell that supports account presentation without registration', () => {
    const call = installBridge(1);
    const hook = renderHook(() => useNativeAccountActionRegistration());

    hook.unmount();
    expect(call).not.toHaveBeenCalled();
  });
});
