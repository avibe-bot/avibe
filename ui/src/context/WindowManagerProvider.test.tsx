// @vitest-environment jsdom

import { act, render } from '@testing-library/react';
import { useEffect } from 'react';
import { describe, expect, it } from 'vitest';

import { useWindowManager, type WindowManagerValue } from './WindowManagerContext';
import { WindowManagerProvider } from './WindowManagerProvider';

describe('WindowManagerProvider focus ownership', () => {
  it('returns focus to the canvas without hiding the active window', () => {
    let manager: WindowManagerValue | null = null;
    const Probe = () => {
      const value = useWindowManager();
      useEffect(() => {
        manager = value;
      }, [value]);
      return null;
    };

    render(
      <WindowManagerProvider>
        <Probe />
      </WindowManagerProvider>,
    );

    let windowId = '';
    act(() => {
      windowId = manager!.openApp('files');
    });
    expect(manager!.focusedId).toBe(windowId);
    expect(manager!.windows.find((window) => window.id === windowId)?.minimized).toBe(false);

    act(() => manager!.focusCanvas());
    expect(manager!.focusedId).toBeNull();
    expect(manager!.windows.find((window) => window.id === windowId)?.minimized).toBe(false);
  });
});
