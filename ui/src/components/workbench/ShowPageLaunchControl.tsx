import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AppWindow,
  ChevronRight,
  ExternalLink,
  Loader2,
  MessageSquare,
  Presentation,
  SquareArrowOutUpRight,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { showPagePrivatePath } from '../../apps/showPageAvatar';
import { showDockId, useDock } from '../../context/DockContext';
import { useShowPageDrag } from '../../context/showPageDrag';
import { useWindowManager } from '../../context/WindowManagerContext';
import {
  isShowPageWindowDrop,
  SHOW_PAGE_DOCK_DROP_SELECTOR,
  showPageWindowOrigin,
  type ViewportPoint,
} from '../../lib/showPageLaunch';
import { Button } from '../ui/button';
import { Popover, PopoverAnchor, PopoverContent } from '../ui/popover';

interface ShowPageLaunchControlProps {
  sessionId: string;
  title: string | null;
  showPageMode: boolean;
  busy: boolean;
  onToggle: () => void;
  /** Ensure the page and send the first-build prompt without changing chat view. */
  onPrepareLaunch: (sessionId: string) => Promise<boolean>;
}

interface ActiveDrag {
  start: ViewportPoint;
}

const HOVER_OPEN_DELAY_MS = 180;
const HOVER_CLOSE_DELAY_MS = 160;

/** Visualize toggle plus hover launch menu and native drag-to-window behavior. */
export const ShowPageLaunchControl: React.FC<ShowPageLaunchControlProps> = ({
  sessionId,
  title,
  showPageMode,
  busy,
  onToggle,
  onPrepareLaunch,
}) => {
  const { t } = useTranslation();
  const { openApp } = useWindowManager();
  const dock = useDock();
  const showPageDrag = useShowPageDrag();
  const [menuOpen, setMenuOpen] = useState(false);
  const [dragCue, setDragCue] = useState<ViewportPoint | null>(null);
  const dragRef = useRef<ActiveDrag | null>(null);
  const suppressClickRef = useRef(false);
  const hoverTimerRef = useRef<number | null>(null);

  const canLaunch = !showPageMode && !busy && Boolean(sessionId);
  const windowTitle = title?.trim() || t('chat.untitled');

  const clearHoverTimer = useCallback(() => {
    if (hoverTimerRef.current === null) return;
    window.clearTimeout(hoverTimerRef.current);
    hoverTimerRef.current = null;
  }, []);

  const openHoverMenu = useCallback(() => {
    if (!canLaunch || dragRef.current) return;
    clearHoverTimer();
    hoverTimerRef.current = window.setTimeout(() => {
      hoverTimerRef.current = null;
      setMenuOpen(true);
    }, HOVER_OPEN_DELAY_MS);
  }, [canLaunch, clearHoverTimer]);

  const closeHoverMenu = useCallback(() => {
    clearHoverTimer();
    hoverTimerRef.current = window.setTimeout(() => {
      hoverTimerRef.current = null;
      setMenuOpen(false);
    }, HOVER_CLOSE_DELAY_MS);
  }, [clearHoverTimer]);

  useEffect(() => () => clearHoverTimer(), [clearHoverTimer]);

  const prepare = useCallback(() => onPrepareLaunch(sessionId), [onPrepareLaunch, sessionId]);

  const openWindow = useCallback(
    async (point?: ViewportPoint) => {
      setMenuOpen(false);
      if (!(await prepare())) return;
      openApp('showpage', {
        title: windowTitle,
        bounds: point ? showPageWindowOrigin(point) : undefined,
        params: { sessionId, title: windowTitle },
      });
    },
    [openApp, prepare, sessionId, windowTitle],
  );

  const openLink = useCallback(async () => {
    setMenuOpen(false);
    // Open synchronously inside the click gesture, then navigate after ensure;
    // otherwise popup blockers reject window.open after the awaited request.
    const tab = window.open('about:blank', '_blank');
    if (!tab) return;
    try {
      tab.opener = null;
    } catch {
      // Some browser WindowProxy implementations expose a read-only opener.
    }
    if (!(await prepare())) {
      tab.close();
      return;
    }
    if (!tab.closed) tab.location.replace(showPagePrivatePath(sessionId));
  }, [prepare, sessionId]);

  const pinToDock = useCallback(async () => {
    if (!(await prepare())) return;
    const dockId = showDockId(sessionId);
    if (dock.isPinned(sessionId)) await dock.dock(dockId);
    else await dock.pin(sessionId);
  }, [dock, prepare, sessionId]);

  useEffect(() => {
    const onDragOver = (event: DragEvent) => {
      const active = dragRef.current;
      if (!active) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest(SHOW_PAGE_DOCK_DROP_SELECTOR)) {
        setDragCue(null);
        return;
      }
      const point = { x: event.clientX, y: event.clientY };
      if (!isShowPageWindowDrop(active.start, point)) {
        setDragCue(null);
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'none';
        return;
      }
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
      setDragCue(point);
    };

    const onDrop = (event: DragEvent) => {
      const active = dragRef.current;
      if (!active) return;
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest(SHOW_PAGE_DOCK_DROP_SELECTOR)) return;
      const point = { x: event.clientX, y: event.clientY };
      if (!isShowPageWindowDrop(active.start, point)) return;
      event.preventDefault();
      dragRef.current = null;
      setDragCue(null);
      showPageDrag.end();
      void openWindow(point);
    };

    window.addEventListener('dragover', onDragOver);
    window.addEventListener('drop', onDrop);
    return () => {
      window.removeEventListener('dragover', onDragOver);
      window.removeEventListener('drop', onDrop);
    };
  }, [openWindow, showPageDrag]);

  const endDrag = () => {
    dragRef.current = null;
    setDragCue(null);
    showPageDrag.end();
    window.setTimeout(() => {
      suppressClickRef.current = false;
    }, 0);
  };

  const label = showPageMode ? t('chat.showPage.backToChat') : t('chat.showPage.open');

  return (
    <>
      <Popover open={canLaunch && menuOpen} onOpenChange={(next) => !next && setMenuOpen(false)}>
        <PopoverAnchor asChild>
          <Button
            type="button"
            variant={showPageMode ? 'secondary' : 'ghost'}
            onClick={() => {
              if (suppressClickRef.current) return;
              setMenuOpen(false);
              onToggle();
            }}
            onMouseEnter={openHoverMenu}
            onMouseLeave={closeHoverMenu}
            onFocus={openHoverMenu}
            draggable={canLaunch}
            onDragStart={(event) => {
              if (!canLaunch || !window.matchMedia?.('(min-width: 768px)').matches) {
                event.preventDefault();
                return;
              }
              clearHoverTimer();
              setMenuOpen(false);
              suppressClickRef.current = true;
              dragRef.current = {
                start: { x: event.clientX, y: event.clientY },
              };
              event.dataTransfer.effectAllowed = 'copy';
              event.dataTransfer.setData('application/x-avibe-show-page', sessionId);
              showPageDrag.begin(pinToDock);
            }}
            onDragEnd={endDrag}
            disabled={busy}
            aria-label={label}
            title={label}
            aria-haspopup={canLaunch ? 'menu' : undefined}
            aria-expanded={canLaunch ? menuOpen : undefined}
            className="h-7 shrink-0 gap-1.5 px-2"
          >
            {busy ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : showPageMode ? (
              <MessageSquare className="size-3.5" />
            ) : (
              <Presentation className="size-3.5" />
            )}
            <span className="hidden text-xs font-medium md:inline">{label}</span>
          </Button>
        </PopoverAnchor>
        <PopoverContent
          align="end"
          side="bottom"
          sideOffset={6}
          className="w-44 p-1"
          onMouseEnter={clearHoverTimer}
          onMouseLeave={closeHoverMenu}
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => event.preventDefault()}
        >
          <div role="menu" aria-label={t('chat.showPage.launchMenu')}>
            <button
              type="button"
              role="menuitem"
              onClick={() => void openWindow()}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] text-foreground transition-colors hover:bg-cyan-soft"
            >
              <AppWindow className="size-3.5 shrink-0 text-cyan" />
              <span className="flex-1">{t('chat.showPage.newWindow')}</span>
              <ChevronRight className="size-3.5 shrink-0 text-muted" />
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => void openLink()}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[12.5px] text-foreground transition-colors hover:bg-cyan-soft"
            >
              <ExternalLink className="size-3.5 shrink-0 text-mint" />
              <span className="flex-1">{t('chat.showPage.newLink')}</span>
              <ChevronRight className="size-3.5 shrink-0 text-muted" />
            </button>
          </div>
        </PopoverContent>
      </Popover>

      {dragCue && (
        <span
          aria-hidden
          style={{ left: dragCue.x + 14, top: dragCue.y + 14 }}
          className="pointer-events-none fixed z-[80] grid size-8 place-items-center rounded-md border border-cyan/60 bg-surface-3 text-cyan shadow-lg"
        >
          <SquareArrowOutUpRight className="size-4" />
        </span>
      )}
    </>
  );
};
