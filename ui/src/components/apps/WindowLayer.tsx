import { useEffect, useRef, useState } from 'react';

import { useWindowManager } from '../../context/WindowManagerContext';
import { clampToLayer } from '../../lib/windowBounds';
import { AppWindow } from './AppWindow';

// The portal layer that hosts app windows. Covers the workbench main area (right
// of the 240px sidebar on desktop). The layer itself is pointer-events-none so
// empty space passes clicks through to the workbench underneath; each AppWindow
// re-enables pointer events on itself. Desktop-only — mobile opens apps full
// screen (P5), so no free-floating windows there.
export const WindowLayer: React.FC = () => {
  const { windows, focusedId, close, minimize, setBounds } = useWindowManager();
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

  // When the layer shrinks (browser narrowed, moved to a smaller display), a
  // window previously dragged near the old right/bottom edge can fall entirely
  // outside the new bounds — and Dock activation would then just focus that
  // off-screen instance instead of a reachable one. Re-clamp visible windows on
  // every layer-size change. Read windows from a ref so this fires only on resize,
  // not on every drag tick (a drag already clamps itself).
  const windowsRef = useRef(windows);
  windowsRef.current = windows;
  useEffect(() => {
    if (size.w === 0 || size.h === 0) return;
    for (const w of windowsRef.current) {
      if (w.minimized || w.maximized) continue;
      const c = clampToLayer(w.bounds, size.w, size.h);
      if (c.x !== w.bounds.x || c.y !== w.bounds.y) setBounds(w.id, c);
    }
  }, [size, setBounds]);

  // ⌘W / Ctrl+W closes the focused window, ⌘M / Ctrl+M minimizes it — but only
  // when DOM focus is genuinely inside a window. `focusedId` alone is just the top
  // z-order window, which lingers after clicking back into the workbench (empty
  // layer space is pointer-events-none), so gating on it would hijack ⌘W while the
  // user types in the chat composer. Requiring real focus inside the layer lets the
  // chord fall through to the browser/page in every other context.
  useEffect(() => {
    if (!focusedId) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      if (!ref.current?.contains(document.activeElement)) return;
      const key = e.key.toLowerCase();
      if (key === 'w') {
        e.preventDefault();
        close(focusedId);
      } else if (key === 'm') {
        e.preventDefault();
        minimize(focusedId);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [focusedId, close, minimize]);

  const visible = windows.filter((w) => !w.minimized);

  return (
    <div
      ref={ref}
      aria-hidden={visible.length === 0}
      className="pointer-events-none fixed inset-y-0 left-0 right-0 z-20 hidden md:left-[240px] md:block"
    >
      {visible.map((w) => (
        <AppWindow key={w.id} win={w} layerWidth={size.w} layerHeight={size.h} />
      ))}
    </div>
  );
};
