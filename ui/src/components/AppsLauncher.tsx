import { useEffect, useRef, useState } from 'react';
import { ChevronUp, LayoutGrid, Pin } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { Dock } from './apps/Dock';

// The sidebar bottom-left "Apps" button that reveals the Dock.
//   - hover  → the Dock floats up ABOVE the button (transient preview; the cursor
//              can move straight onto it). Mirrors InboxHoverPopover's timer dance.
//   - click  → pin / unpin toggle (sticky). Unpinning hides it immediately even if
//              the cursor is still on the button (suppressed until it leaves).
export const AppsLauncher: React.FC = () => {
  const { t } = useTranslation();
  const [pinned, setPinned] = useState(false);
  const [hovering, setHovering] = useState(false);
  const closeTimer = useRef<number | null>(null);
  // Set on unpin so the lingering hover doesn't immediately re-open the panel;
  // cleared once the cursor actually leaves the trigger+panel.
  const suppressHover = useRef(false);

  const visible = pinned || hovering;

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

  return (
    // z-30 keeps the Apps button (and its Dock popover) above the window layer (z-20) so it stays
    // reachable even under a MAXIMIZED window (design If1Tt). The sidebar aside is intentionally
    // un-stacked, so this z-30 composites at the root, above the window layer.
    <div className="relative z-30 flex-1" onMouseEnter={openHover} onMouseLeave={queueClose}>
      <button
        type="button"
        onClick={onClick}
        aria-haspopup="menu"
        aria-expanded={visible}
        aria-pressed={pinned}
        className={clsx(
          'group flex w-full items-center gap-2.5 rounded-full border bg-cyan-soft px-4 py-2.5 text-[13px] font-bold text-foreground transition-colors',
          visible
            ? 'border-cyan shadow-[0_0_22px_-4px_rgba(63,224,229,0.7)]'
            : 'border-cyan/45 shadow-[0_0_14px_-5px_rgba(63,224,229,0.55)] hover:border-cyan/70',
        )}
      >
        <LayoutGrid className="size-4 shrink-0 text-cyan" />
        <span className="flex-1 whitespace-nowrap text-left">{t('apps.title')}</span>
        {pinned ? (
          <Pin className="size-3.5 shrink-0 rotate-45 fill-cyan text-cyan" />
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
    </div>
  );
};
