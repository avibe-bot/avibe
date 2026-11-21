/** @vitest-environment jsdom */
import { render, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useWindowCloseGuard } from '../context/WindowManagerContext';
import { WindowManagerProvider } from '../context/WindowManagerProvider';

import { useNativeWindowCloseGuard, type NativeWindowCloseConfirmation } from './nativeWindowCloseGuard';

vi.mock('react-i18next', async (importOriginal) => ({
  ...(await importOriginal<typeof import('react-i18next')>()),
  useTranslation: () => ({ t: (key: string) => key }),
}));

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

function ManagedBody({ id, message }: { id: string; message: string | null }) {
  useWindowCloseGuard(id, message);
  return null;
}
const managed = (children: ReactNode) => (
  <MemoryRouter><WindowManagerProvider standalone>{children}</WindowManagerProvider></MemoryRouter>
);

describe('windows managed inside a native App window', () => {
  it('make the native close and quit ask while any managed window is unsaved', () => {
    nativeWindow();
    const view = render(managed(<>
      <ManagedBody id="win-files" message={null} />
      <ManagedBody id="win-editor" message="Discard unsaved changes and close this window?" />
    </>));
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.()).toEqual({
      message: 'Discard unsaved changes and close this window?',
      confirm: 'apps.editor.discardAndClose',
      cancel: 'common.cancel',
    });
    view.rerender(managed(<>
      <ManagedBody id="win-files" message={null} />
      <ManagedBody id="win-editor" message={null} />
    </>));
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.()).toBeNull();
    view.rerender(managed(<ManagedBody id="win-editor" message="unsaved" />));
    view.unmount();
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__?.()).toBeNull();
  });

  it('leave the Workbench window to its own guards', () => {
    render(managed(<ManagedBody id="win-editor" message="unsaved" />));
    expect(window.__AVIBE_APP_WINDOW_CLOSE_GUARD__).toBeUndefined();
  });
});
