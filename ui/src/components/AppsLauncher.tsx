import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevronUp, LayoutGrid, Pin } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Dock } from './apps/Dock';
import { ContextMenu, ContextMenuItem } from './ui/context-menu';
import { useWindowManager } from '../context/WindowManagerContext';
import { useShowPageDrag } from '../context/showPageDrag';

// The sidebar bottom-left "Apps" button that reveals the Dock.
//   - hover        → the Dock floats up ABOVE the button (transient preview; the
//                    cursor can move straight onto it). Mirrors InboxHoverPopover.
//   - click        → pin / unpin toggle (sticky). Unpinning hides it immediately
//                    even if the cursor is still on the button.
//   - right-click  → a context menu with an "Open App Library" escape hatch — a
//                    third way to reach the Library (alongside the sidebar entry
//                    and the empty-Dock hint), complementing §7.1c point 7. It
//                    does NOT touch the hover/pin behavior.
export const AppsLauncher: React.FC = () => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const showPageDrag = useShowPageDrag();
  const [pinned, setPinned] = useState(false);
  const [hovering, setHovering] = useState(false);
  const [dragHovering, setDragHovering] = useState(false);
  // Cursor-positioned right-click menu, on the shared ContextMenu primitive.
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const closeTimer = useRef<number | null>(null);
  const slotRef = useRef<HTMLDivElement | null>(null);
  const launcherRef = useRef<HTMLDivElement | null>(null);
  const [placement, setPlacement] = useState({ left: 0, bottom: 0, width: 0, height: 0 });
  // Set on unpin so the lingering hover doesn't immediately re-open the panel;
  // cleared once the cursor actually leaves the trigger+panel.
  const suppressHover = useRef(false);

  const visible = pinned || hovering || (showPageDrag.active && dragHovering);

  useLayoutEffect(() => {
    const slot = slotRef.current;
    const launcher = launcherRef.current;
    if (!slot || !launcher) return;
    const measure = () => {
      const rect = slot.getBoundingClientRect();
      const next = {
        left: rect.left,
        bottom: window.innerHeight - rect.bottom,
        width: rect.width,
        height: launcher.getBoundingClientRect().height,
      };
      setPlacement((current) =>
        current.left === next.left && current.bottom === next.bottom
          && current.width === next.width && current.height === next.height ? current : next,
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(slot);
    observer.observe(launcher);
    // The optional hostname/version rows can move the slot without resizing it.
    if (slot.parentElement?.parentElement) observer.observe(slot.parentElement.parentElement);
    window.addEventListener('resize', measure);
    window.addEventListener('scroll', measure, true);
    return () => {
      observer.disconnect();
      window.removeEventListener('resize', measure);
      window.removeEventListener('scroll', measure, true);
    };
  }, []);

  useEffect(() => {
    const resetDragHover = () => setDragHovering(false);
    window.addEventListener('dragend', resetDragHover);
    return () => window.removeEventListener('dragend', resetDragHover);
  }, []);

  const openHover = () => {
    if (suppressHover.current) return;
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
    setHovering(true);
  };
  const queueClose = () => {
    if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => {
      setHovering(false);
      suppressHover.current = false;
      closeTimer.current = null;
    }, 180);
  };
  useEffect(
    () => () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
    },
    [],
  );

  const onClick = () => {
    if (pinned) {
      setPinned(false);
      setHovering(false);
      suppressHover.current = true;
    } else {
      setPinned(true);
    }
  };

  const openLibrary = () => {
    wm.openApp('library');
    setMenu(null);
  };

  const launcher = (
    <div
      ref={launcherRef}
      className="fixed z-30 hidden md:block"
      style={{ left: placement.left, bottom: placement.bottom, width: placement.width }}
      data-show-page-dock-drop-target
      onMouseEnter={openHover}
      onMouseLeave={queueClose}
      onDragEnter={(event) => {
        if (!showPageDrag.active) return;
        event.preventDefault();
        setDragHovering(true);
      }}
      onDragOver={(event) => {
        if (!showPageDrag.active) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
        setDragHovering(true);
      }}
      onDragLeave={(event) => {
        const next = event.relatedTarget;
        if (next instanceof Node && event.currentTarget.contains(next)) return;
        setDragHovering(false);
      }}
      onDrop={(event) => {
        if (!showPageDrag.active) return;
        event.preventDefault();
        event.stopPropagation();
        setDragHovering(false);
        // Keep the Dock visible just long enough for the optimistic pin to land
        // and be seen. Entering the Dock cancels this timer via openHover.
        setHovering(true);
        if (closeTimer.current !== null) window.clearTimeout(closeTimer.current);
        closeTimer.current = window.setTimeout(() => {
          setHovering(false);
          closeTimer.current = null;
        }, 1200);
        showPageDrag.dropToDock();
      }}
    >
      <button
        type="button"
        onClick={onClick}
        onContextMenu={(e) => {
          e.preventDefault();
          setMenu({ x: e.clientX, y: e.clientY });
        }}
        aria-haspopup="menu"
        aria-expanded={visible}
        aria-pressed={pinned}
        className={clsx(
          'group flex w-full items-center gap-2.5 rounded-full border bg-cyan-soft px-4 py-2.5 text-[13px] font-bold text-foreground transition-colors',
          visible
            ? 'border-cyan shadow-glow-md-cyan'
            : 'border-cyan/45 shadow-glow-sm-cyan hover:border-cyan/70',
        )}
      >
        <LayoutGrid className="size-4 shrink-0 text-cyan-ink" />
        <span className="flex-1 whitespace-nowrap text-left">{t('apps.title')}</span>
        {pinned ? (
          <Pin className="size-3.5 shrink-0 rotate-45 fill-cyan text-cyan-ink" />
        ) : (
          <ChevronUp className={clsx('size-3.5 shrink-0 text-muted transition-transform', !visible && 'rotate-180')} />
        )}
      </button>

      {visible && (
        <div
          role="menu"
          aria-label={t('apps.title')}
          onMouseEnter={openHover}
          onMouseLeave={queueClose}
          className="absolute bottom-full left-0 z-50 mb-2"
        >
          <Dock />
        </div>
      )}

      {menu && (
        <ContextMenu x={menu.x} y={menu.y} onClose={() => setMenu(null)} width={184} itemCount={1}>
          <ContextMenuItem
            icon={<LayoutGrid className="size-[15px] text-cyan-ink" />}
            label={t('apps.launcher.openLibrary')}
            onClick={openLibrary}
          />
        </ContextMenu>
      )}
    </div>
  );

  return (
    <>
      {/* Keep the sidebar's layout slot, but let this single launcher and its Dock escape the
          sidebar stacking context. z-30 is above all app windows (z-20), below dialogs (z-50). */}
      <div ref={slotRef} className="flex-1" style={{ height: placement.height }} />
      {createPortal(launcher, document.body)}
    </>
  );
};
