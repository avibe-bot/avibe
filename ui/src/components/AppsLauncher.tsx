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
    <div className="relative flex-1" onMouseEnter={openHover} onMouseLeave={queueClose}>
      <button
        type="button"
        onClick={onClick}
        aria-haspopup="menu"
        aria-expanded={visible}
        aria-pressed={pinned}
        className={clsx(
          'group flex w-full items-center gap-2.5 rounded-lg border px-3 py-2.5 text-[13px] font-medium transition-colors',
          visible
            ? 'border-cyan/40 bg-cyan-soft text-foreground shadow-[0_0_16px_-4px_rgba(63,224,229,0.5)]'
            : 'border-border-strong text-foreground hover:bg-foreground/[0.04]',
        )}
      >
        <LayoutGrid className={clsx('size-4', visible ? 'text-cyan' : 'text-muted group-hover:text-foreground')} />
        <span className="flex-1 text-left">{t('apps.title')}</span>
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
