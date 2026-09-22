// @vitest-environment jsdom

import { act, render } from '@testing-library/react';
import { useEffect } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { RouteSurfaceActiveContext } from '../lib/routeSurfaceActivity';
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

    // Restoring and maximizing are ways to the top like any other — both raise
    // the window — and must not be left out.
    act(() => manager().minimize(second));
    act(() => manager().restore(second));
    expect(onForeground).toHaveBeenCalledTimes(4);

    act(() => manager().toggleMaximize(first));
    expect(onForeground).toHaveBeenCalledTimes(5);
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

// Clearing the way is the one thing a window manager does on someone else's
// behalf, so it is the one thing that depends on who is asking. A route retained
// behind the Settings overlay is still mounted and still running; if it reaches
// the manager anyway it must get its window without taking the overlay down with
// it. The gate lives in `useWindowManager` so no caller has to know it exists.
describe('useWindowManager foreground ownership', () => {
  const renderManager = (onWindowForeground: () => void) => {
    let manager: WindowManagerValue | null = null;
    const Probe = () => {
      const value = useWindowManager();
      useEffect(() => {
        manager = value;
      }, [value]);
      return null;
    };
    const view = render(
      <RouteSurfaceActiveContext.Provider value>
        <WindowManagerProvider onWindowForeground={onWindowForeground}>
          <Probe />
        </WindowManagerProvider>
      </RouteSurfaceActiveContext.Provider>,
    );
    return {
      manager: () => manager!,
      setSurfaceActive: (active: boolean) => view.rerender(
        <RouteSurfaceActiveContext.Provider value={active}>
          <WindowManagerProvider onWindowForeground={onWindowForeground}>
            <Probe />
          </WindowManagerProvider>
        </RouteSurfaceActiveContext.Provider>,
      ),
    };
  };

  it('stops announcing once the caller is no longer the live surface', () => {
    const onForeground = vi.fn();
    const { manager, setSurfaceActive } = renderManager(onForeground);

    act(() => void manager().openApp('files'));
    expect(onForeground).toHaveBeenCalledTimes(1);

    act(() => setSurfaceActive(false));
    onForeground.mockClear();

    let id = '';
    act(() => {
      id = manager().openApp('files');
    });
    act(() => manager().focus(id));
    act(() => manager().minimize(id));
    act(() => manager().restore(id));

    // Only the announcement is withheld. The window still opened and still came
    // to the top — it is simply waiting behind whatever the user is looking at,
    // which is exactly where an obsolete caller's window belongs.
    expect(onForeground).not.toHaveBeenCalled();
    expect(manager().windows).toHaveLength(2);
    expect(manager().focusedId).toBe(id);
  });

  it('judges the call, not the closure it was made in', () => {
    const onForeground = vi.fn();
    const { manager, setSurfaceActive } = renderManager(onForeground);

    // What an awaited launch holds: `ShowPageLaunchControl` reads `openApp` off
    // the manager, awaits `prepare()`, and calls it afterwards. The user opening
    // Settings mid-await retires the surface without touching this reference.
    const suspendedLaunch = manager().openApp;

    act(() => setSurfaceActive(false));
    act(() => void suspendedLaunch('files'));
    expect(onForeground).not.toHaveBeenCalled();

    // The same reference, once its surface is the live one again — so the test
    // above is about ownership at the moment of the call, not a stale closure
    // that stopped working.
    act(() => setSurfaceActive(true));
    act(() => void suspendedLaunch('files'));
    expect(onForeground).toHaveBeenCalledTimes(1);
  });
});
