import { useState, type ReactNode } from 'react';
import { DesktopWindowChromeContext } from '../context/DesktopWindowChromeContext';
import { desktopDragRegion } from '../lib/desktopShell';

/**
 * A window-level home for the existing chat toolbar, beside native traffic lights.
 *
 * Native chrome stacking contract:
 *
 * | region | boundary | precedence |
 * | safe strip | y < Hn (`--shell-titlebar-inset`) | native chrome / resizer |
 * | workbench | y >= Hn | WindowLayer (z-20) > toolbar (z-18) |
 *
 * Ht (`--desktop-chat-toolbar-h`, 48px) remains the toolbar's layout height.
 * It must not become the WindowLayer safety boundary.
 */
export function DesktopWindowChrome({ enabled, hidden, children }: {
  enabled: boolean;
  hidden: boolean;
  children: ReactNode;
}) {
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  return (
    <DesktopWindowChromeContext.Provider value={enabled ? slot : null}>
      {enabled && (
        <div
          data-desktop-window-chrome=""
          hidden={hidden}
          inert={hidden || undefined}
          className="fixed inset-x-0 top-0 z-[18] h-[var(--desktop-chat-toolbar-h)]"
        >
          {/* Native traffic lights occupy the left of the sidebar; its remaining
              width is a drag target. No transparent overlay covers controls. */}
          <div
            aria-hidden="true"
            data-tauri-drag-region={desktopDragRegion()}
            className="absolute inset-y-0 left-0 w-[var(--app-sidebar-w)] select-none"
          />
          <div
            ref={setSlot}
            data-tauri-drag-region={desktopDragRegion()}
            className="ml-[var(--app-sidebar-w)] h-full"
          />
        </div>
      )}
      {children}
    </DesktopWindowChromeContext.Provider>
  );
}
