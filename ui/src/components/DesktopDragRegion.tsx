import clsx from 'clsx';
import { desktopDragRegion } from '../lib/desktopShell';

/** Makes existing traffic-light clearance draggable without adding layout height. */
export function DesktopDragRegion({ className }: { className: string }) {
  const region = desktopDragRegion();
  if (region === undefined) return null;
  return <div aria-hidden="true" data-tauri-drag-region={region} className={clsx('h-[var(--shell-titlebar-inset)] select-none', className)} />;
}
