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
    const hook = renderHook(() => useNativeAccountActionRegistration(true));

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
    const hook = renderHook(() => useNativeAccountActionRegistration(true));

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
    const hook = renderHook(() => useNativeAccountActionRegistration(true));

    hook.unmount();
    expect(call).not.toHaveBeenCalled();
  });

  it('publishes the same availability used by the visible header action', () => {
    const call = installBridge();
    const hook = renderHook(
      ({ available }) => useNativeAccountActionRegistration(available),
      { initialProps: { available: false } },
    );

    expect(call.mock.calls).toEqual([
      ['app.setAccountActionAvailable', { available: false }],
    ]);

    hook.rerender({ available: true });
    hook.rerender({ available: false });
    expect(call.mock.calls).toEqual([
      ['app.setAccountActionAvailable', { available: false }],
      ['app.setAccountActionAvailable', { available: true }],
      ['app.setAccountActionAvailable', { available: false }],
    ]);

    hook.unmount();
    expect(call).toHaveBeenLastCalledWith(
      'app.setAccountActionAvailable',
      { available: false },
    );
  });

  it('restores the current availability after a page-cache round trip', () => {
    const call = installBridge();
    const hook = renderHook(
      ({ available }) => useNativeAccountActionRegistration(available),
      { initialProps: { available: false } },
    );

    act(() => window.dispatchEvent(new Event('pagehide')));
    hook.rerender({ available: true });
    expect(call.mock.calls.every(([, params]) => params.available === false)).toBe(true);

    act(() => window.dispatchEvent(new Event('pageshow')));
    expect(call).toHaveBeenLastCalledWith(
      'app.setAccountActionAvailable',
      { available: true },
    );

    hook.unmount();
    expect(call).toHaveBeenLastCalledWith(
      'app.setAccountActionAvailable',
      { available: false },
    );
  });
});
