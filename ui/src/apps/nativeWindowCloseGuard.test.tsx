/** @vitest-environment jsdom */
import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { useNativeWindowCloseGuard, type NativeWindowCloseConfirmation } from './nativeWindowCloseGuard';

const discard: NativeWindowCloseConfirmation = {
  message: 'Discard unsaved changes and close this window?',
  confirm: 'Discard and Close',
  cancel: 'Cancel',
};

function nativeWindow() {
  Object.defineProperty(window, '__AVIBE_NATIVE_APP_WINDOW__', { value: true, configurable: true });
}

afterEach(() => {
  delete (window as { __AVIBE_NATIVE_APP_WINDOW__?: true }).__AVIBE_NATIVE_APP_WINDOW__;
  delete window.__AVIBE_APP_WINDOW_CLOSE_GUARD__;
});

describe('useNativeWindowCloseGuard', () => {
  it('answers the shell with the confirmation only while there is unsaved state', () => {
    nativeWindow();
    const { rerender, unmount } = renderHook(({ value }) => useNativeWindowCloseGuard(value), {
      initialProps: { value: null as NativeWindowCloseConfirmation | null },
    });
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.() ?? null).toBeNull();
    rerender({ value: discard });
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.()).toEqual(discard);
    rerender({ value: null });
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.()).toBeNull();
    rerender({ value: discard });
    unmount();
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.()).toBeNull();
  });

  it('keeps asking while any registered body is still unsaved', () => {
    nativeWindow();
    const first = renderHook(() => useNativeWindowCloseGuard(discard));
    const second = renderHook(() => useNativeWindowCloseGuard({ ...discard, message: 'Second' }));
    first.unmount();
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.()).toEqual({ ...discard, message: 'Second' });
    second.unmount();
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.()).toBeNull();
  });

  it('leaves browser tabs and the Workbench to their own unload guards', () => {
    renderHook(() => useNativeWindowCloseGuard(discard));
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__).toBeUndefined();
  });
});
