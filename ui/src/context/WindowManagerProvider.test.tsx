// @vitest-environment jsdom

import { act, render } from '@testing-library/react';
import { useEffect } from 'react';
import { describe, expect, it, vi } from 'vitest';

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

  it('transfers focus to the highest visible window when the owner closes or minimizes', () => {
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

    let firstId = '';
    let secondId = '';
    act(() => {
      firstId = manager!.openApp('files');
      secondId = manager!.openApp('files');
    });
    expect(manager!.focusedId).toBe(secondId);

    act(() => manager!.close(secondId));
    expect(manager!.focusedId).toBe(firstId);

    let thirdId = '';
    act(() => {
      thirdId = manager!.openApp('files');
    });
    expect(manager!.focusedId).toBe(thirdId);

    act(() => manager!.minimize(thirdId));
    expect(manager!.focusedId).toBe(firstId);
  });
});

// The shell can have the whole window layer hidden — Settings does — so a window
// brought forward would arrive invisible. Rather than teach every caller to ask
// what is covering the layer, the manager announces that a window is coming
// forward and the shell clears the way. Announcing it late would be the same bug
// with extra steps, so it goes first, before the state that raises the window.
describe('WindowManagerProvider foreground announcements', () => {
  const renderManager = (onWindowForeground: () => void) => {
    let manager: WindowManagerValue | null = null;
    const Probe = () => {
      const value = useWindowManager();
      useEffect(() => {
        manager = value;
      }, [value]);
      return null;
    };

    render(
      <WindowManagerProvider onWindowForeground={onWindowForeground}>
        <Probe />
      </WindowManagerProvider>,
    );
    return () => manager!;
  };

  it('announces every way a window reaches the top', () => {
    const onForeground = vi.fn();
    const manager = renderManager(onForeground);

    let first = '';
    let second = '';
    act(() => {
      first = manager().openApp('files');
      second = manager().openApp('files');
    });
    expect(onForeground).toHaveBeenCalledTimes(2);

    act(() => manager().focus(first));
    expect(onForeground).toHaveBeenCalledTimes(3);

    // Restoring goes through `focus`, so it needs no announcement of its own —
    // and must not be left without one either.
    act(() => manager().minimize(second));
    act(() => manager().restore(second));
    expect(onForeground).toHaveBeenCalledTimes(4);
  });

  it('leaves the ways down alone', () => {
    const onForeground = vi.fn();
    const manager = renderManager(onForeground);

    let id = '';
    act(() => {
      id = manager().openApp('files');
    });
    onForeground.mockClear();

    // Minimize and close do re-focus whatever was underneath, but nothing new
    // comes forward from behind the shell's own surfaces, so there is nothing
    // to clear out of the way.
    act(() => manager().focusCanvas());
    act(() => manager().minimize(id));
    act(() => manager().close(id));
    expect(onForeground).not.toHaveBeenCalled();
  });

  it('works without a listener at all', () => {
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

    let id = '';
    act(() => {
      id = manager!.openApp('files');
    });
    expect(manager!.focusedId).toBe(id);
  });
});
