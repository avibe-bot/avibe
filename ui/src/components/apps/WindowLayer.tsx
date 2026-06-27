import { useEffect, useRef, useState } from 'react';

import { useWindowManager } from '../../context/WindowManagerContext';
import { AppWindow } from './AppWindow';

// Inside a text-entry surface (xterm's hidden textarea, Monaco's textarea, any
// input / contenteditable), Ctrl is a control character — ^W deletes a word, ^M is
// Enter — so the window chord must never hijack it there. Only the Mac Meta chord
// (⌘W/⌘M) acts as a window command on a text surface.
function onTextSurface(el: Element | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  return el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.isContentEditable;
}

// The portal layer that hosts app windows. Covers the workbench main area (right
// of the 240px sidebar on desktop). The layer itself is pointer-events-none so
// empty space passes clicks through to the workbench underneath; each AppWindow
// re-enables pointer events on itself (minimized windows stay mounted but inert,
// so their terminal/editor state survives a minimize). Desktop-only — mobile opens
// apps full screen (P5), so no free-floating windows there.
export const WindowLayer: React.FC = () => {
  const { windows, focusedId, close, minimize, confirmClose } = useWindowManager();
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

  // ⌘W / Ctrl+W closes the focused window, ⌘M / Ctrl+M minimizes it — but only when
  // DOM focus is genuinely inside a window. `focusedId` alone is just the top z-order
  // window, which lingers after clicking back into the workbench (empty layer space
  // is pointer-events-none), so gating on it would hijack ⌘W while the user types in
  // the chat composer. Requiring real focus inside the layer lets the chord fall
  // through to the browser/page everywhere else; and on a text surface only Meta
  // counts, so terminal/editor Ctrl chords reach the app (see onTextSurface).
  useEffect(() => {
    if (!focusedId) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
      const active = document.activeElement;
      if (!ref.current?.contains(active)) return;
      if (e.ctrlKey && !e.metaKey && onTextSurface(active)) return;
      const key = e.key.toLowerCase();
      if (key === 'w') {
        e.preventDefault();
        if (confirmClose(focusedId)) close(focusedId);
      } else if (key === 'm') {
        e.preventDefault();
        minimize(focusedId);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [focusedId, close, minimize, confirmClose]);

  // Render every window — minimized ones stay mounted (hidden + inert via AppWindow)
  // so their app body keeps its state. The layer is only aria-hidden when nothing is
  // actually shown.
  const anyShown = windows.some((w) => !w.minimized);

  return (
    <div
      ref={ref}
      aria-hidden={!anyShown}
      className="pointer-events-none fixed inset-y-0 left-0 right-0 z-20 hidden md:left-[240px] md:block"
    >
      {windows.map((w) => (
        <AppWindow key={w.id} win={w} layerWidth={size.w} layerHeight={size.h} />
      ))}
    </div>
  );
};
