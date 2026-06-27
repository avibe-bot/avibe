import { useEffect, useRef, useState } from 'react';

import { useWindowManager } from '../../context/WindowManagerContext';
import { AppWindow } from './AppWindow';

// The portal layer that hosts app windows. Covers the workbench main area (right
// of the 240px sidebar on desktop). The layer itself is pointer-events-none so
// empty space passes clicks through to the workbench underneath; each AppWindow
// re-enables pointer events on itself. Desktop-only — mobile opens apps full
// screen (P5), so no free-floating windows there.
export const WindowLayer: React.FC = () => {
  const { windows } = useWindowManager();
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
