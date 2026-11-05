/** @vitest-environment jsdom */

import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useNativeOuterScrollLock } from './useNativeOuterScrollLock';

const installBridge = (withViewport = true) => {
  const call = vi.fn(async () => null);
  Object.defineProperty(window, 'avibeNative', {
    configurable: true,
    value: {
      contract: 1,
      platform: 'ios',
      shellVersion: '1.0 (1)',
      capabilities: withViewport ? { viewport: 1 } : { app: 1 },
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

describe('useNativeOuterScrollLock', () => {
  it('locks for the mounted Workbench lifetime and releases on unmount', () => {
    const call = installBridge();
    const hook = renderHook(() => useNativeOuterScrollLock());

    expect(call).toHaveBeenCalledWith(
      'viewport.setOuterScrollLocked',
      { locked: true },
    );

    hook.unmount();
    expect(call).toHaveBeenLastCalledWith(
      'viewport.setOuterScrollLocked',
      { locked: false },
    );
    expect(call).toHaveBeenCalledTimes(2);
  });

  it('releases once on pagehide and does not duplicate the release on unmount', () => {
    const call = installBridge();
    const hook = renderHook(() => useNativeOuterScrollLock());

    act(() => {
      window.dispatchEvent(new Event('pagehide'));
    });
    hook.unmount();

    expect(call).toHaveBeenCalledTimes(2);
    expect(call).toHaveBeenLastCalledWith(
      'viewport.setOuterScrollLocked',
      { locked: false },
    );
  });

  it('reacquires the lock when WebKit restores the Workbench from its page cache', () => {
    const call = installBridge();
    const hook = renderHook(() => useNativeOuterScrollLock());

    act(() => {
      window.dispatchEvent(new Event('pagehide'));
      window.dispatchEvent(new Event('pageshow'));
    });

    expect(call.mock.calls).toEqual([
      ['viewport.setOuterScrollLocked', { locked: true }],
      ['viewport.setOuterScrollLocked', { locked: false }],
      ['viewport.setOuterScrollLocked', { locked: true }],
    ]);

    hook.unmount();
    expect(call).toHaveBeenLastCalledWith(
      'viewport.setOuterScrollLocked',
      { locked: false },
    );
  });

  it('does not call an older shell without the viewport capability', () => {
    const call = installBridge(false);
    const hook = renderHook(() => useNativeOuterScrollLock());

    hook.unmount();
    expect(call).not.toHaveBeenCalled();
  });
});
