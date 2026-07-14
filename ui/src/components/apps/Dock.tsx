import { useMemo, useRef, useState } from 'react';
import { Reorder } from 'framer-motion';
import { Copy, ExternalLink, LayoutGrid, PinOff, Plus, SquarePlus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { APP_REGISTRY, type AppDefinition, type AppId } from '../../apps/registry';
import { showPageAvatar, showPageIconUrl, showPagePrivatePath } from '../../apps/showPageAvatar';
import { ShowPageAvatarContent } from '../../apps/showPageAvatarTile';
import { dockIdToSession, useDock } from '../../context/DockContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import { ContextMenu, ContextMenuItem } from '../ui/context-menu';
import { useShowPageInventory } from '../useShowPages';
import { isDragRelease } from './dragClick';

// A resident Dock tile resolved from a persisted Dock id: either a built-in app
// (files/terminal/editor) or a pinned Show Page (`show:<session_id>`).
type ResidentItem =
  | { kind: 'builtin'; id: string; def: AppDefinition }
  | { kind: 'showpage'; id: string; sessionId: string; title: string; iconVersion: string | null };

// The unified Dock panel: app launcher + running indicators + minimized windows.
// Built-in apps and pinned Show Pages share one reorderable row (server-persisted
// order); a running app's tile reveals a ＋ (and a right-click menu) to open
// another window. EVERY tile — built-ins included (§7.1c) — can be unpinned from
// the Dock via the right-click menu (it stays installed, so the Apps view can
// re-dock it); pinned Show Pages additionally offer Open in New Tab. When the Dock
// is empty it shows an App Library shortcut rather than a dead surface. The
// minimized-window strip trails the row and is not reorderable.
export const Dock: React.FC = () => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const { order, pins, undock, setOrder } = useDock();
  const { pages } = useShowPageInventory();
  const { windows, focusedId } = wm;
  const minimized = windows.filter((w) => w.minimized);

  // Cursor-positioned right-click menu, on the shared ContextMenu primitive.
  const [menu, setMenu] = useState<{ x: number; y: number; item: ResidentItem } | null>(null);

  const pinBySession = useMemo(() => new Map(pins.map((pin) => [pin.session_id, pin])), [pins]);
  const pageBySession = useMemo(() => new Map(pages.map((page) => [page.session_id, page])), [pages]);

  const resolveItem = useMemo(() => {
    return (id: string): ResidentItem | null => {
      const sessionId = dockIdToSession(id);
      if (sessionId !== null) {
        const page = pageBySession.get(sessionId);
        const title = page
          ? page.title?.trim() || t('chat.untitled')
          : pinBySession.get(sessionId)?.title_snapshot?.trim() || sessionId;
        return { kind: 'showpage', id, sessionId, title, iconVersion: page?.icon_version ?? null };
      }
      const def = APP_REGISTRY[id as AppId];
      return def ? { kind: 'builtin', id, def } : null;
    };
  }, [pageBySession, pinBySession, t]);

  // Local, drag-mutable copy of the order; framer's Reorder updates it live, and
  // it re-syncs whenever the server order changes (load / pin / unpin elsewhere).
  const [dragOrder, setDragOrder] = useState<string[] | null>(null);
  const dragRef = useRef<string[] | null>(null);
  // Last pointer-press point on a tile, to tell a reorder drag from a click on
  // release (§7.1e item 4b): the whole tile is BOTH draggable and click-openable.
  const pressPtRef = useRef<{ x: number; y: number } | null>(null);
  const localOrder = dragOrder ?? order;
  const reorder = (next: string[]) => {
    dragRef.current = next;
    setDragOrder(next);
  };

  // Persist on drop — only when the order actually changed (a plain click can
  // fire a spurious drag-end), so an unchanged reorder makes no request.
  const commitOrder = () => {
    const next = dragRef.current ?? order;
    dragRef.current = null;
    setDragOrder(null);
    if (next.length === order.length && next.every((id, i) => id === order[i])) return;
    void setOrder(next);
  };

  const windowsFor = (item: ResidentItem) =>
    item.kind === 'builtin'
      ? windows.filter((w) => w.appId === item.id)
      : windows.filter((w) => w.appId === 'showpage' && w.params?.sessionId === item.sessionId);

  const focusWindowDom = (id: string) =>
    (document.querySelector(`[data-window-id="${id}"]`) as HTMLElement | null)?.focus?.({ preventScroll: true });

  const openNew = (item: ResidentItem) => {
    if (item.kind === 'builtin') wm.openApp(item.id as AppId);
    else wm.openApp('showpage', { title: item.title, params: { sessionId: item.sessionId, title: item.title } });
    setMenu(null);
  };

  // Click a tile: focus its front window, else un-minimize its top window, else
  // launch one — matched per Show Page by session id (multi-window like built-ins).
  const activate = (item: ResidentItem) => {
    setMenu(null);
    const own = windowsFor(item);
    const visible = own.filter((w) => !w.minimized);
    if (visible.length > 0) {
      const top = visible.reduce((a, b) => (b.z > a.z ? b : a));
      wm.focus(top.id);
      focusWindowDom(top.id);
    } else if (own.length > 0) {
      wm.restore(own.reduce((a, b) => (b.z > a.z ? b : a)).id);
    } else {
      openNew(item);
    }
  };

  const showAll = (item: ResidentItem) => {
    const own = windowsFor(item);
    own.filter((w) => w.minimized).forEach((w) => wm.restore(w.id));
    const visible = own.filter((w) => !w.minimized);
    if (visible.length > 0) wm.focus(visible.reduce((a, b) => (b.z > a.z ? b : a)).id);
    setMenu(null);
  };

  const openExternal = (sessionId: string) => {
    window.open(showPagePrivatePath(sessionId), '_blank', 'noopener,noreferrer');
    setMenu(null);
  };

  // Unpin from the Dock = undock (remove from `order`) for ANY tile, built-ins
  // included. It keeps the app installed (built-ins always; a Show Page stays in
  // the Apps list), so the tile can be re-docked from the Library — uninstalling
  // a page is the separate 移出 action there.
  const undockItem = (dockId: string) => {
    void undock(dockId);
    setMenu(null);
  };

  const focusedApp = windows.find((w) => w.id === focusedId);
  const isActive = (item: ResidentItem) =>
    !!focusedApp &&
    (item.kind === 'builtin'
      ? focusedApp.appId === item.id
      : focusedApp.appId === 'showpage' && focusedApp.params?.sessionId === item.sessionId);

  const menuItemCount = (item: ResidentItem) => {
    const running = windowsFor(item).length > 0;
    // New Window (+ Show All Windows if running) + [Open in New Tab for a
    // Show Page] + Unpin from Dock (every tile).
    return 1 + (running ? 1 : 0) + (item.kind === 'showpage' ? 1 : 0) + 1;
  };

  return (
    <div className="relative">
      {/* Bound the panel to the viewport and scroll horizontally: with many pinned
          apps the resident row would otherwise run off-screen (the popover sits at
          the bottom-left), leaving later tiles unreachable for open/reorder/unpin. */}
      <div className="relative flex max-w-[min(88vw,880px)] items-end gap-2 overflow-x-auto overscroll-x-contain rounded-2xl border border-border-strong bg-surface-2/95 p-2 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.65)] backdrop-blur-xl">
        <Reorder.Group axis="x" values={localOrder} onReorder={reorder} as="div" className="flex items-end gap-2">
          {localOrder.map((id, index) => {
            const item = resolveItem(id);
            if (!item) return null;
            const running = windowsFor(item).length > 0;
            const active = isActive(item);
            const label = item.kind === 'builtin' ? t(item.def.titleKey) : item.title;
            const avatar = item.kind === 'showpage' ? showPageAvatar(item.sessionId, item.title) : null;
            const BuiltinIcon = item.kind === 'builtin' ? item.def.icon : null;
            const dotColor = item.kind === 'builtin' ? `var(${item.def.accent})` : `var(${avatar!.accentVar})`;

            return (
              <Reorder.Item
                key={id}
                value={id}
                as="div"
                onDragEnd={commitOrder}
                className="group/tile relative flex w-[60px] cursor-grab flex-col items-center gap-1 active:cursor-grabbing"
              >
                {/* Icon wrapper positions the ＋ affordance to the icon's corner. */}
                <div className="relative">
                  <button
                    type="button"
                    title={
                      index < 9 ? `${label} · ${t('apps.dock.switchShortcut', { number: index + 1 })}` : label
                    }
                    aria-label={label}
                    onPointerDown={(e) => {
                      pressPtRef.current = { x: e.clientX, y: e.clientY };
                    }}
                    onClick={(e) => {
                      // After a reorder drag the browser still fires a click on
                      // release; swallow it past the drag threshold so the tile
                      // doesn't spuriously open. Read-and-null the press point, and
                      // skip the check for keyboard/synthetic activation (detail 0,
                      // clientX/Y 0, no fresh pointerdown) so Enter/Space still opens.
                      const press = pressPtRef.current;
                      pressPtRef.current = null;
                      if (e.detail !== 0 && isDragRelease(press, { x: e.clientX, y: e.clientY })) return;
                      if (e.metaKey || e.ctrlKey || e.altKey) openNew(item);
                      else activate(item);
                    }}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      setMenu({ x: e.clientX, y: e.clientY, item });
                    }}
                    className={clsx(
                      'grid size-10 place-items-center overflow-hidden rounded-xl border text-[15px] font-bold leading-none transition',
                      item.kind === 'builtin' &&
                        (active ? 'border-2 bg-foreground/[0.07]' : 'border-border bg-foreground/[0.03] hover:bg-foreground/[0.07]'),
                    )}
                    style={
                      item.kind === 'builtin'
                        ? active
                          ? { borderColor: `var(${item.def.accent})` }
                          : undefined
                        : {
                            // Pinned Show Page: a letter avatar on a hashed accent tint (no icon
                            // pipeline). Border brightens to the full accent when focused.
                            color: `var(${avatar!.accentVar})`,
                            backgroundColor: `color-mix(in srgb, var(${avatar!.accentVar}) 16%, transparent)`,
                            borderWidth: active ? 2 : 1,
                            borderColor: active
                              ? `var(${avatar!.accentVar})`
                              : `color-mix(in srgb, var(${avatar!.accentVar}) 34%, transparent)`,
                          }
                    }
                  >
                    {item.kind === 'builtin' && BuiltinIcon ? (
                      <BuiltinIcon className="size-5" style={{ color: `var(${item.def.accent})` }} />
                    ) : (
                      <ShowPageAvatarContent
                        iconUrl={item.kind === 'showpage' ? showPageIconUrl(item.sessionId, item.iconVersion) : null}
                        letter={avatar!.letter}
                      />
                    )}
                  </button>

                  {/* ＋ reveals on hover and opens another window directly. */}
                  <button
                    type="button"
                    title={t('apps.dock.newWindow')}
                    aria-label={t('apps.dock.newWindow')}
                    onClick={(e) => {
                      e.stopPropagation();
                      openNew(item);
                    }}
                    // Don't let a press on ＋ start a tile drag.
                    onPointerDown={(e) => e.stopPropagation()}
                    className="absolute -right-1 -top-1 grid size-4 place-items-center rounded-full border border-border-strong bg-surface-2 text-muted opacity-0 shadow transition hover:text-foreground group-hover/tile:opacity-100"
                  >
                    <Plus className="size-2.5" strokeWidth={3} />
                  </button>
                </div>

                <span className="max-w-full truncate text-[10px] leading-tight text-muted">{label}</span>
                <span className="size-1 rounded-full" style={{ backgroundColor: running ? dotColor : 'transparent' }} />
              </Reorder.Item>
            );
          })}
        </Reorder.Group>

        {/* Empty Dock (every tile undocked) is valid — offer a way back to the App
            Library instead of a dead, empty surface (§7.1c). */}
        {localOrder.length === 0 && (
          <button
            type="button"
            onClick={() => {
              wm.openApp('library');
              setMenu(null);
            }}
            className="flex items-center gap-2 rounded-xl border border-dashed border-border px-3 py-2.5 text-[12px] font-medium text-muted transition-colors hover:border-cyan/60 hover:text-foreground"
          >
            <LayoutGrid className="size-4 shrink-0 text-cyan" />
            <span className="whitespace-nowrap">{t('apps.dock.emptyHint')}</span>
          </button>
        )}

        {minimized.length > 0 && <div className="mx-1 h-11 w-px shrink-0 self-center bg-border-strong" />}

        {/* Minimized windows render as a small window-style thumbnail (a mini titlebar with traffic
            lights + the app icon) with the title stacked BELOW it — matching the app tiles' layout so
            the whole row stays vertically consistent. Not reorderable. */}
        {minimized.map((w) => {
          const def = APP_REGISTRY[w.appId];
          const Icon = def.icon;
          const label = w.title ?? t(def.titleKey);
          return (
            <button
              key={w.id}
              type="button"
              title={label}
              aria-label={t('apps.window.restore', { defaultValue: 'Restore' })}
              onClick={() => wm.restore(w.id)}
              className="group/min flex w-[64px] shrink-0 flex-col items-center gap-1"
            >
              <span className="flex h-9 w-14 flex-col overflow-hidden rounded-md border border-border-strong bg-surface shadow-sm transition group-hover/min:border-foreground/40">
                {/* Faux window titlebar with the three traffic lights. */}
                <span className="flex h-2.5 shrink-0 items-center gap-[3px] border-b border-border/70 bg-surface-2 px-1">
                  <span className="size-1 rounded-full" style={{ backgroundColor: '#ff5f57' }} />
                  <span className="size-1 rounded-full" style={{ backgroundColor: '#febc2e' }} />
                  <span className="size-1 rounded-full" style={{ backgroundColor: '#28c840' }} />
                </span>
                <span className="grid flex-1 place-items-center">
                  <Icon className="size-4" style={{ color: `var(${def.accent})` }} />
                </span>
              </span>
              <span className="max-w-full truncate text-[10px] leading-tight text-muted">{label}</span>
            </button>
          );
        })}
      </div>

      {menu &&
        (() => {
          // Bind the target to a const so the discriminated-union narrowing
          // survives into the item onClick closures (a mutable `menu.item` would
          // widen back to ResidentItem inside them).
          const item = menu.item;
          const showpage = item.kind === 'showpage' ? item : null;
          return (
            <ContextMenu x={menu.x} y={menu.y} onClose={() => setMenu(null)} width={196} itemCount={menuItemCount(item)}>
              <ContextMenuItem
                icon={<SquarePlus className="size-[15px] text-cyan" />}
                label={t('apps.dock.newWindow')}
                onClick={() => openNew(item)}
              />
              {windowsFor(item).length > 0 && (
                <ContextMenuItem
                  icon={<Copy className="size-[15px]" />}
                  label={t('apps.dock.showAllWindows')}
                  onClick={() => showAll(item)}
                />
              )}
              {showpage && (
                <ContextMenuItem
                  icon={<ExternalLink className="size-[15px]" />}
                  label={t('apps.dock.openInNewTab')}
                  onClick={() => openExternal(showpage.sessionId)}
                />
              )}
              {/* Unpin (undock) is available on EVERY tile now, built-ins included. */}
              <ContextMenuItem
                icon={<PinOff className="size-[15px]" />}
                label={t('apps.dock.unpin')}
                danger
                onClick={() => undockItem(item.id)}
              />
            </ContextMenu>
          );
        })()}
    </div>
  );
};
