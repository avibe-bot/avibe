import { useEffect, useMemo, useRef, useState } from 'react';
import { Reorder } from 'framer-motion';
import { Copy, ExternalLink, PinOff, Plus, SquarePlus } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { APP_REGISTRY, type AppDefinition, type AppId } from '../../apps/registry';
import { showPageAvatar, showPagePrivatePath } from '../../apps/showPageAvatar';
import { dockIdToSession, useDock } from '../../context/DockContext';
import { useWindowManager } from '../../context/WindowManagerContext';
import { ContextMenu, ContextMenuItem } from '../ui/context-menu';

// A resident Dock tile resolved from a persisted Dock id: either a built-in app
// (files/terminal/editor) or a pinned Show Page (`show:<session_id>`).
type ResidentItem =
  | { kind: 'builtin'; id: string; def: AppDefinition }
  | { kind: 'showpage'; id: string; sessionId: string; title: string };

// The unified Dock panel: app launcher + running indicators + minimized windows.
// Built-in apps and pinned Show Pages share one reorderable row (server-persisted
// order); a running app's tile reveals a ＋ (and a right-click menu) to open
// another window. Pinned Show Pages add Open in New Tab / Unpin from Dock; the
// minimized-window strip trails the row and is not reorderable.
export const Dock: React.FC = () => {
  const { t } = useTranslation();
  const wm = useWindowManager();
  const { order, pins, unpin, setOrder } = useDock();
  const { windows, focusedId } = wm;
  const minimized = windows.filter((w) => w.minimized);

  // Cursor-positioned right-click menu, on the shared ContextMenu primitive.
  const [menu, setMenu] = useState<{ x: number; y: number; item: ResidentItem } | null>(null);

  const pinBySession = useMemo(() => new Map(pins.map((pin) => [pin.session_id, pin])), [pins]);

  const resolveItem = useMemo(() => {
    return (id: string): ResidentItem | null => {
      const sessionId = dockIdToSession(id);
      if (sessionId !== null) {
        const title = pinBySession.get(sessionId)?.title_snapshot?.trim() || sessionId;
        return { kind: 'showpage', id, sessionId, title };
      }
      const def = APP_REGISTRY[id as AppId];
      return def ? { kind: 'builtin', id, def } : null;
    };
  }, [pinBySession]);

  // Local, drag-mutable copy of the order; framer's Reorder updates it live, and
  // it re-syncs whenever the server order changes (load / pin / unpin elsewhere).
  const [localOrder, setLocalOrder] = useState<string[]>(order);
  useEffect(() => setLocalOrder(order), [order]);
  const localRef = useRef(localOrder);
  localRef.current = localOrder;

  // Persist on drop — only when the order actually changed (a plain click can
  // fire a spurious drag-end), so an unchanged reorder makes no request.
  const commitOrder = () => {
    const next = localRef.current;
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

  const unpinItem = (sessionId: string) => {
    void unpin(sessionId);
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
    return 1 + (running ? 1 : 0) + (item.kind === 'showpage' ? 2 : 0);
  };

  return (
    <div className="relative">
      {/* Bound the panel to the viewport and scroll horizontally: with many pinned
          apps the resident row would otherwise run off-screen (the popover sits at
          the bottom-left), leaving later tiles unreachable for open/reorder/unpin. */}
      <div className="relative flex max-w-[min(88vw,880px)] items-end gap-2 overflow-x-auto overscroll-x-contain rounded-2xl border border-border-strong bg-surface-2/95 p-2 shadow-[0_24px_64px_-12px_rgba(0,0,0,0.65)] backdrop-blur-xl">
        <Reorder.Group axis="x" values={localOrder} onReorder={setLocalOrder} as="div" className="flex items-end gap-2">
          {localOrder.map((id) => {
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
                    title={label}
                    aria-label={label}
                    onClick={(e) => (e.metaKey || e.ctrlKey || e.altKey ? openNew(item) : activate(item))}
                    onContextMenu={(e) => {
                      e.preventDefault();
                      setMenu({ x: e.clientX, y: e.clientY, item });
                    }}
                    className={clsx(
                      'grid size-10 place-items-center rounded-xl border transition',
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
                      <span className="text-[15px] font-bold leading-none">{avatar!.letter}</span>
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
                <>
                  <ContextMenuItem
                    icon={<ExternalLink className="size-[15px]" />}
                    label={t('apps.dock.openInNewTab')}
                    onClick={() => openExternal(showpage.sessionId)}
                  />
                  <ContextMenuItem
                    icon={<PinOff className="size-[15px]" />}
                    label={t('apps.dock.unpin')}
                    danger
                    onClick={() => unpinItem(showpage.sessionId)}
                  />
                </>
              )}
            </ContextMenu>
          );
        })()}
    </div>
  );
};
