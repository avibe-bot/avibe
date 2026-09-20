import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AppWindow,
  ExternalLink,
  Loader2,
  MessageSquare,
  Presentation,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { appTabHref } from '../../apps/appLaunch';
import { useDock } from '../../context/DockContext';
import { showDockId } from '../../context/dockDoc';
import { useShowPageDrag } from '../../context/showPageDrag';
import { useWindowManager } from '../../context/WindowManagerContext';
import {
  isShowPageWindowDrop,
  SHOW_PAGE_DOCK_DROP_SELECTOR,
  showPageWindowOrigin,
  type ViewportPoint,
} from '../../lib/showPageLaunch';
import { isDesktopViewport } from '../../lib/useIsDesktop';
import { useRouteSurfaceWindowEvent } from '../../lib/routeSurfaceActivity';
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
  const { openApp, setGestureActive } = useWindowManager();
  const dock = useDock();
  const { begin: beginShowPageDrag, end: endShowPageDrag } = useShowPageDrag();
  const [menuOpen, setMenuOpen] = useState(false);
  const dragRef = useRef<ActiveDrag | null>(null);
  const suppressClickRef = useRef(false);
  const hoverTimerRef = useRef<number | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const canLaunch = !showPageMode && !busy && Boolean(sessionId);
  const windowTitle = title?.trim() || t('chat.untitled');
  const linkHref = appTabHref({ appId: 'showpage', sessionId });

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

  useEffect(
    () => () => {
      clearHoverTimer();
      if (dragRef.current) {
        endShowPageDrag();
        setGestureActive(false);
      }
    },
    [clearHoverTimer, endShowPageDrag, setGestureActive],
  );

  const prepare = useCallback(() => onPrepareLaunch(sessionId), [onPrepareLaunch, sessionId]);

  const openWindow = useCallback(
    async (point?: ViewportPoint) => {
      clearHoverTimer();
      setMenuOpen(false);
      if (!(await prepare())) return;
      openApp('showpage', {
        title: windowTitle,
        bounds: point ? showPageWindowOrigin(point) : undefined,
        params: { sessionId, title: windowTitle },
      });
    },
    [clearHoverTimer, openApp, prepare, sessionId, windowTitle],
  );

  const openLink = useCallback(async () => {
    clearHoverTimer();
    setMenuOpen(false);
    if (!linkHref) return;
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
    if (!tab.closed) tab.location.replace(linkHref);
  }, [clearHoverTimer, linkHref, prepare]);

  const pinToDock = useCallback(async () => {
    if (!(await prepare())) return;
    const dockId = showDockId(sessionId);
    if (dock.isPinned(sessionId)) await dock.dock(dockId);
    else await dock.pin(sessionId);
  }, [dock, prepare, sessionId]);

  useRouteSurfaceWindowEvent('dragover', (event) => {
    const active = dragRef.current;
    if (!active) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest(SHOW_PAGE_DOCK_DROP_SELECTOR)) {
      return;
    }
    const point = { x: event.clientX, y: event.clientY };
    if (!isShowPageWindowDrop(active.start, point)) {
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'none';
      return;
    }
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  });

  useRouteSurfaceWindowEvent('drop', (event) => {
    const active = dragRef.current;
    if (!active) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest(SHOW_PAGE_DOCK_DROP_SELECTOR)) return;
    const point = { x: event.clientX, y: event.clientY };
    if (!isShowPageWindowDrop(active.start, point)) return;
    event.preventDefault();
    dragRef.current = null;
    endShowPageDrag();
    setGestureActive(false);
    void openWindow(point);
  });

  const endDrag = () => {
    dragRef.current = null;
    endShowPageDrag();
    setGestureActive(false);
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
              clearHoverTimer();
              if (suppressClickRef.current) return;
              setMenuOpen(false);
              onToggle();
            }}
            onKeyDown={(event) => {
              if (!canLaunch || event.key !== 'ArrowDown') return;
              event.preventDefault();
              clearHoverTimer();
              setMenuOpen(true);
              window.requestAnimationFrame(() => {
                menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
              });
            }}
            onMouseEnter={openHoverMenu}
            onMouseLeave={closeHoverMenu}
            onFocus={openHoverMenu}
            onBlur={(event) => {
              clearHoverTimer();
              const next = event.relatedTarget;
              if (next instanceof Node && menuRef.current?.contains(next)) return;
              setMenuOpen(false);
            }}
            draggable={canLaunch}
            onDragStart={(event) => {
              if (!canLaunch || !isDesktopViewport()) {
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
              // Reuse the window gesture shield so a drop over an app iframe
              // still reaches this document's dragover/drop listeners.
              setGestureActive(true);
              beginShowPageDrag(pinToDock);
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
          className="w-fit min-w-32 p-1"
          onMouseEnter={clearHoverTimer}
          onMouseLeave={closeHoverMenu}
          onOpenAutoFocus={(event) => event.preventDefault()}
          onCloseAutoFocus={(event) => event.preventDefault()}
        >
          <div
            ref={menuRef}
            role="menu"
            aria-label={t('chat.showPage.launchMenu')}
            onBlur={(event) => {
              const next = event.relatedTarget;
              if (next instanceof Node && event.currentTarget.contains(next)) return;
              clearHoverTimer();
              setMenuOpen(false);
            }}
            onKeyDown={(event) => {
              if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
              const items = Array.from(
                event.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]'),
              );
              if (items.length === 0) return;
              event.preventDefault();
              const current = items.indexOf(document.activeElement as HTMLElement);
              if (event.key === 'Home') items[0]?.focus();
              else if (event.key === 'End') items.at(-1)?.focus();
              else if (event.key === 'ArrowDown') items[(current + 1) % items.length]?.focus();
              else items[(current - 1 + items.length) % items.length]?.focus();
            }}
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => void openWindow()}
              className="flex w-full items-center gap-2 whitespace-nowrap rounded-md px-2 py-1.5 text-left text-[12.5px] text-foreground transition-colors hover:bg-cyan-soft"
            >
              <AppWindow className="size-3.5 shrink-0 text-cyan-ink" />
              <span>{t('chat.showPage.newWindow')}</span>
            </button>
            {linkHref && (
              <button
                type="button"
                role="menuitem"
                onClick={() => void openLink()}
                className="flex w-full items-center gap-2 whitespace-nowrap rounded-md px-2 py-1.5 text-left text-[12.5px] text-foreground transition-colors hover:bg-cyan-soft"
              >
                <ExternalLink className="size-3.5 shrink-0 text-mint-ink" />
                <span>{t('chat.showPage.newLink')}</span>
              </button>
            )}
          </div>
        </PopoverContent>
      </Popover>

    </>
  );
};
