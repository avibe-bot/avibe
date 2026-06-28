import { useEffect, useRef, useState } from 'react';

import { useWindowManager } from '../../context/WindowManagerContext';
import { AppWindow } from './AppWindow';

// In the TERMINAL, Ctrl is a control-character stream — ^W deletes a word, ^M is
// carriage return — so the window chord must never hijack Ctrl there (xterm focuses a
// hidden textarea inside its `.xterm` root). The editor is the opposite: Monaco has no
// useful Ctrl+W, so we WANT Ctrl+W to close its window (guarded for unsaved edits)
// rather than be swallowed and bypass the prompt — hence the exemption is terminal-only.
function inTerminalSurface(el: Element | null): boolean {
  return el instanceof HTMLElement && !!el.closest('.xterm');
}

// The portal layer that hosts app windows. Covers the workbench main area (right
// of the 240px sidebar on desktop). The layer itself is pointer-events-none so
// empty space passes clicks through to the workbench underneath; each AppWindow
// re-enables pointer events on itself (minimized windows stay mounted but inert,
// so their terminal/editor state survives a minimize). Desktop-only — mobile opens
// apps full screen (P5), so no free-floating windows there.
export const WindowLayer: React.FC = () => {
  const { windows, close, minimize, confirmClose } = useWindowManager();
  const ref = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // ⌘W / Ctrl+W closes — ⌘M / Ctrl+M minimizes — the window the keyboard is actually
  // in, resolved from the DOM-focused element's `data-window-id`. NOT the top z-order
  // window: focus can tab or programmatically move into a lower window, and acting on
  // the top one would then close/minimize the wrong window (a dirty editor or terminal
  // the user isn't in). When focus isn't inside any window — the chat composer, another
  // page — the chord falls through to the browser. Inside the terminal only Meta counts,
  // so its Ctrl control-chars (^W/^M) reach the shell (see inTerminalSurface).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      const active = document.activeElement;
      const winEl = active instanceof Element ? active.closest('[data-window-id]') : null;
      if (!winEl || !ref.current?.contains(winEl)) return;
      if (e.ctrlKey && !e.metaKey && inTerminalSurface(active)) return;
      const targetId = winEl.getAttribute('data-window-id');
      if (!targetId) return;
      const key = e.key.toLowerCase();
      if (key === 'w') {
        e.preventDefault();
        if (confirmClose(targetId)) close(targetId);
      } else if (key === 'm') {
        e.preventDefault();
        minimize(targetId);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [close, minimize, confirmClose]);

  // Render every window — minimized ones stay mounted (hidden + inert via AppWindow)
  // so their app body keeps its state. The layer is only aria-hidden when nothing is
  // actually shown.
  const anyShown = windows.some((w) => !w.minimized);

  return (
    <div
      ref={ref}
      aria-hidden={!anyShown}
      // The windowed apps (Editor / Terminal / File Browser) are ALWAYS dark — they're
      // dark-only in the design (dnYPx / iwYIX / nknn2) with hardcoded-dark content areas
      // (Monaco, xterm), so a light global theme would otherwise paint light chrome around
      // dark content (and make the welcome text unreadable). `data-theme="dark"` re-cascades
      // the dark token set to this whole subtree regardless of the app theme.
      data-theme="dark"
      // Spans the FULL viewport (no longer offset past the sidebar): windows can move over
      // the sidebar and maximize fills the whole screen. The sidebar's bottom Apps/Dock
      // cluster sits above this layer (z-30) so it stays reachable under a maximized window.
      className="pointer-events-none fixed inset-0 z-20 hidden md:block"
    >
      {windows.map((w) => (
        <AppWindow key={w.id} win={w} layerWidth={size.w} layerHeight={size.h} />
      ))}
    </div>
  );
};
