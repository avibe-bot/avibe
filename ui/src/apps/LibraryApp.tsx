import { useCallback, useMemo, useRef, useState } from 'react';
import { Reorder, useDragControls } from 'framer-motion';
import { GripVertical, Minus, Pin, PinOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import clsx from 'clsx';

import { APP_LIST, APP_REGISTRY, type AppDefinition, type AppId } from './registry';
import { deriveAppRows, partitionByDock, type AppRow } from './appLibrary';
import { ShowPageAvatarTile } from './showPageAvatarTile';
import { showAppRoutePath } from '../components/apps/mobileDock';
import { useDock } from '../context/DockContext';
import { useWindowManager } from '../context/WindowManagerContext';
import { ShowPagesView } from '../components/ShowPagesPage';
import { useShowPages, type ShowPage } from '../components/useShowPages';
import { Badge } from '../components/ui/badge';
import { Button } from '../components/ui/button';
import { SearchField } from '../components/settings/SettingsPrimitives';

// The App Library: the app manager, itself a built-in app (§7.1). Two views over
// the two-layer state (§7.1c): the INSTALLED set (Apps) and the full Show Pages
// inventory (AI). "Installed" (a member of `pins`, plus the always-installed
// built-ins) is now distinct from "docked" (a member of the Dock `order`): every
// row opens the app on click and carries a state-aware dock/undock action that
// keeps it in the list, and AI-kind rows additionally get a 移出 (uninstall).
// Renders as a window body (desktop) and as a full-screen route (mobile), so
// `windowId`/`params` are accepted but unused.

// The client's built-in Dock ids (files / terminal / editor / library), in
// canonical order. Mirrors DockContext's BUILTIN_DOCK_IDS. The Library now lists
// itself among them (§7.1c #7), so nothing is excluded.
const BUILTIN_DOCK_IDS: string[] = APP_LIST.map((app) => app.id);

type LibraryTab = 'apps' | 'showpages';

/**
 * Open an app the way the current surface can show it. On desktop that's a
 * workbench window (`openApp`). On mobile there is no window layer, so built-ins
 * navigate to their in-shell `/apps/*` route and a Show Page navigates to the
 * full-screen `/apps/show/:sessionId` route (§7.1b) — both keep the AppShell
 * chrome instead of dropping the user onto the raw page or a new browser tab.
 */
function useOpenApp() {
  const wm = useWindowManager();
  const navigate = useNavigate();
  return useCallback(
    (appId: AppId, opts?: { title?: string; sessionId?: string }) => {
      const desktop = typeof window !== 'undefined' && !!window.matchMedia?.('(min-width: 768px)').matches;
      if (desktop) {
        wm.openApp(appId, {
          title: opts?.title,
          params: opts?.sessionId ? { sessionId: opts.sessionId, title: opts.title } : undefined,
        });
        return;
      }
      if (appId === 'showpage') {
        if (opts?.sessionId) navigate(showAppRoutePath(opts.sessionId));
      } else {
        navigate(`/apps/${appId}`);
      }
    },
    [wm, navigate],
  );
}

export const LibraryApp: React.FC<{ windowId?: string; params?: Record<string, unknown>; initialTab?: LibraryTab }> = ({
  params,
  initialTab,
}) => {
  const { t } = useTranslation();
  const controller = useShowPages();
  const { order, pins } = useDock();
  const openApp = useOpenApp();
  // The legacy /admin/show-pages redirect (mobile via prop, desktop window via
  // params) can request the AI (Show Pages) tab up front; default to Apps otherwise.
  const startTab: LibraryTab = initialTab === 'showpages' || params?.initialTab === 'showpages' ? 'showpages' : 'apps';
  const [tab, setTab] = useState<LibraryTab>(startTab);

  // Honor an external tab request — the /admin/show-pages redirect focusing an
  // already-open window bumps params.navKey — by adjusting state during render
  // (React's recommended alternative to a prop-syncing effect).
  const [seenNavKey, setSeenNavKey] = useState(params?.navKey);
  if (params?.navKey !== seenNavKey) {
    setSeenNavKey(params?.navKey);
    const navTab = params?.navTab;
    if (navTab === 'apps' || navTab === 'showpages') setTab(navTab);
  }

  const appsCount = useMemo(() => deriveAppRows(BUILTIN_DOCK_IDS, pins, order).length, [pins, order]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface text-foreground">
      <div className="flex shrink-0 items-center gap-1 border-b border-border px-3 py-2.5 sm:px-4">
        <TabButton active={tab === 'apps'} onClick={() => setTab('apps')} label={t('library.tab.apps')} count={appsCount} />
        <TabButton
          active={tab === 'showpages'}
          onClick={() => setTab('showpages')}
          label={t('library.tab.ai')}
          count={controller.pages.length}
        />
      </div>
      <div className="min-h-0 flex-1">
        {tab === 'apps' ? (
          <AppsView pages={controller.pages} openApp={openApp} />
        ) : (
          <ShowPagesView {...controller} onOpenApp={(sessionId, title) => openApp('showpage', { sessionId, title })} />
        )}
      </div>
    </div>
  );
};

const TabButton: React.FC<{ active: boolean; onClick: () => void; label: string; count: number }> = ({
  active,
  onClick,
  label,
  count,
}) => (
  <button
    type="button"
    onClick={onClick}
    aria-pressed={active}
    className={clsx(
      'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] transition-colors',
      active ? 'bg-foreground/[0.06] font-semibold text-foreground' : 'font-medium text-muted hover:text-foreground',
    )}
  >
    <span>{label}</span>
    <span className={clsx('font-mono text-[11px]', active ? 'text-muted' : 'text-muted/70')}>· {count}</span>
  </button>
);

interface ResolvedRow {
  row: AppRow;
  name: string;
  subtitle: string;
  /** The built-in app definition (icon + accent) when this row is a built-in. */
  def?: AppDefinition;
  /** The AI page icon's opaque cache token, when it has one (§7.1f). */
  iconVersion?: string | null;
}

// A drag handle for a docked row (§7.1e). Handle-only drag: the Reorder.Item is
// `dragListener={false}`, so a drag starts ONLY from this grip — a plain click
// on the row still opens the app. `touch-none` stops a touch-drag from being
// stolen by the scroll container.
const GripHandle: React.FC<{ controls: ReturnType<typeof useDragControls> }> = ({ controls }) => {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      aria-label={t('library.apps.reorder')}
      title={t('library.apps.reorder')}
      onPointerDown={(e) => {
        e.stopPropagation();
        controls.start(e);
      }}
      onClick={(e) => e.stopPropagation()}
      className="grid size-6 shrink-0 cursor-grab touch-none place-items-center rounded-md text-muted/50 transition-colors hover:text-foreground active:cursor-grabbing"
    >
      <GripVertical size={15} />
    </button>
  );
};

interface AppLibraryRowProps {
  item: ResolvedRow;
  /** Leading slot: a drag handle for docked rows in reorder mode, or a matching
   *  spacer for undocked rows so both groups keep one aligned icon column. */
  leading?: React.ReactNode;
  /** The last row overall carries no bottom divider (single-list look). */
  last?: boolean;
  onOpen: () => void;
  onDockToggle: () => void;
  onRemove?: () => void;
}

// One Apps-view row: icon/avatar, then the info area (name + subtitle) as the
// flex-1 column so the kind badge is right-aligned at the info area's right edge,
// before the action controls (§7.1e); then the kind badge, a state-aware Dock
// toggle (row stays listed either way), and — AI rows only — a 移出 (uninstall;
// the page itself is untouched). Shared by the reorder path (wrapped in a
// Reorder.Item) and the search path (static).
const AppLibraryRow: React.FC<AppLibraryRowProps> = ({ item, leading, last, onOpen, onDockToggle, onRemove }) => {
  const { t } = useTranslation();
  const { row, name, subtitle, def, iconVersion } = item;
  const Icon = def?.icon;
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault();
          onOpen();
        }
      }}
      className={clsx(
        'flex w-full cursor-pointer items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-foreground/[0.02] sm:gap-4 sm:px-5',
        !last && 'border-b border-border',
      )}
    >
      {leading}
      {def && Icon ? (
        <span
          className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-border"
          style={{ color: `var(${def.accent})`, backgroundColor: `color-mix(in srgb, var(${def.accent}) 14%, transparent)` }}
        >
          <Icon className="size-[18px]" />
        </span>
      ) : (
        <ShowPageAvatarTile sessionId={row.sessionId ?? ''} title={name} iconVersion={iconVersion} />
      )}
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-[13px] font-semibold text-foreground">{name}</span>
        {subtitle ? <span className="truncate font-mono text-[11px] text-muted">{subtitle}</span> : null}
      </span>
      {/* Kind badge — right-aligned at the info area's right edge (§7.1e); the AI
          badge replaces the former Show Page badge. */}
      {row.kind === 'builtin' ? (
        <Badge variant="outline" className="hidden font-mono text-[10px] uppercase tracking-wide sm:inline-flex">
          {t('library.kind.builtin')}
        </Badge>
      ) : (
        <Badge variant="success" className="hidden sm:inline-flex">
          {t('library.kind.showPage')}
        </Badge>
      )}
      {/* State-aware Dock toggle — the row stays in the list either way. */}
      <Button
        type="button"
        variant={row.docked ? 'secondary' : 'outline'}
        size="xs"
        onClick={(e) => {
          e.stopPropagation();
          onDockToggle();
        }}
      >
        {row.docked ? <PinOff /> : <Pin />}
        <span className="hidden sm:inline">{row.docked ? t('library.apps.undock') : t('library.apps.dock')}</span>
      </Button>
      {/* 移出 — uninstall (AI rows only; the page itself is untouched). A minus,
          not a trash: this removes the app from the list, it is not a delete. */}
      {row.kind === 'showpage' && onRemove ? (
        <button
          type="button"
          title={t('library.apps.remove')}
          aria-label={t('library.apps.remove')}
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          className="grid size-8 shrink-0 place-items-center rounded-lg text-muted transition-colors hover:bg-destructive/10 hover:text-destructive"
        >
          <Minus size={15} />
        </button>
      ) : null}
    </div>
  );
};

// A docked row wrapped for drag-reorder: the Reorder.Item is handle-only
// (`dragListener={false}` + this row's own dragControls, started from the grip),
// so dragging reorders the docked group while a plain click still opens the app.
// The dockId is the Reorder value, so the group's order IS the Dock order.
const DockedReorderRow: React.FC<{
  item: ResolvedRow;
  last?: boolean;
  onOpen: () => void;
  onDockToggle: () => void;
  onRemove?: () => void;
  onCommit: () => void;
}> = ({ item, last, onOpen, onDockToggle, onRemove, onCommit }) => {
  const controls = useDragControls();
  // A handle drag can end with its trailing click landing on the row (the common
  // ancestor of the grip and the release point), which would open the app right
  // after a reorder (Codex P2). Set on drag start, consumed by the row click; a
  // fresh row-body press resets it (a grip press stopPropagations, so it won't).
  const draggedRef = useRef(false);
  return (
    <Reorder.Item
      value={item.row.dockId}
      as="div"
      dragListener={false}
      dragControls={controls}
      onPointerDown={() => {
        draggedRef.current = false;
      }}
      onDragStart={() => {
        draggedRef.current = true;
      }}
      onDragEnd={onCommit}
    >
      <AppLibraryRow
        item={item}
        last={last}
        leading={<GripHandle controls={controls} />}
        onOpen={() => {
          if (draggedRef.current) {
            draggedRef.current = false;
            return;
          }
          onOpen();
        }}
        onDockToggle={onDockToggle}
        onRemove={onRemove}
      />
    </Reorder.Item>
  );
};

// Apps view: the INSTALLED set (built-ins + installed Show Pages). Rows split
// into the DOCKED group — drag-reorderable via a front grip handle, its order
// persisted straight through the Dock's `setOrder` (PUT /api/dock/order,
// optimistic + stale_order resync) — and the installed-but-undocked group below
// (no handle, stable order). Every row opens the app on click and carries a
// state-aware Dock toggle (固定到 Dock ↔ 取消固定) that never removes it from the
// list; AI rows add a 移出 that uninstalls the app (the page itself is untouched).
// Searching disables reorder (a filtered subset can't safely rewrite the whole
// Dock order) and falls back to a flat static list.
const AppsView: React.FC<{ pages: ShowPage[]; openApp: ReturnType<typeof useOpenApp> }> = ({ pages, openApp }) => {
  const { t } = useTranslation();
  const { order, pins, dock, undock, unpin, setOrder } = useDock();
  const [query, setQuery] = useState('');

  // Local, drag-mutable copy of the docked order; framer's Reorder mutates it
  // live during a drag, and we persist on drop — same pattern as Dock.tsx.
  const [dragOrder, setDragOrder] = useState<string[] | null>(null);
  const dragRef = useRef<string[] | null>(null);

  const pinBySession = useMemo(() => new Map(pins.map((p) => [p.session_id, p])), [pins]);
  const pageBySession = useMemo(() => new Map(pages.map((p) => [p.session_id, p])), [pages]);
  const rows = useMemo(() => deriveAppRows(BUILTIN_DOCK_IDS, pins, order), [pins, order]);

  const resolved = useMemo<ResolvedRow[]>(
    () =>
      rows.map((row) => {
        if (row.kind === 'builtin') {
          const def = APP_REGISTRY[row.builtinId as AppId];
          return { row, name: def ? t(def.titleKey) : (row.builtinId ?? row.dockId), subtitle: t('library.apps.system'), def };
        }
        const sid = row.sessionId ?? '';
        const page = pageBySession.get(sid);
        const name = page?.title?.trim() || pinBySession.get(sid)?.title_snapshot?.trim() || sid;
        const subtitle = page
          ? [page.platform ? t(`platform.${page.platform}.title`, { defaultValue: page.platform }) : null, page.agent]
              .filter(Boolean)
              .join(' · ')
          : '';
        return { row, name, subtitle, iconVersion: page?.icon_version ?? null };
      }),
    [rows, pageBySession, pinBySession, t],
  );
  const resolvedById = useMemo(() => new Map(resolved.map((item) => [item.row.dockId, item])), [resolved]);

  const { docked, undocked } = useMemo(() => partitionByDock(rows, order), [rows, order]);
  const dockedIds = useMemo(() => docked.map((r) => r.dockId), [docked]);

  const q = query.trim().toLowerCase();
  const searching = q.length > 0;
  const searchResults = useMemo(
    () => (searching ? resolved.filter((item) => item.name.toLowerCase().includes(q)) : []),
    [resolved, searching, q],
  );

  // framer's Reorder mutates this local order live; persist on drop. Because the
  // docked group's values ARE the Dock order, `next` is the full new order.
  const localOrder = dragOrder ?? dockedIds;
  const reorder = (next: string[]) => {
    dragRef.current = next;
    setDragOrder(next);
  };
  const commitOrder = () => {
    const next = dragRef.current;
    dragRef.current = null;
    setDragOrder(null);
    // No-op an unchanged reorder (a stray drag-end on a plain click).
    if (!next || (next.length === dockedIds.length && next.every((id, i) => id === dockedIds[i]))) return;
    void setOrder(next);
  };

  const openRow = (row: AppRow) =>
    row.kind === 'builtin'
      ? openApp(row.builtinId as AppId)
      : openApp('showpage', {
          sessionId: row.sessionId ?? '',
          title: pageBySession.get(row.sessionId ?? '')?.title ?? undefined,
        });
  const handlers = (item: ResolvedRow) => ({
    onOpen: () => openRow(item.row),
    onDockToggle: () => void (item.row.docked ? undock(item.row.dockId) : dock(item.row.dockId)),
    onRemove: item.row.kind === 'showpage' && item.row.sessionId ? () => void unpin(item.row.sessionId!) : undefined,
  });

  // The last row overall (search list, else undocked tail, else docked tail).
  const lastDockId = searching
    ? searchResults.at(-1)?.row.dockId
    : (undocked.at(-1)?.dockId ?? localOrder.at(-1));

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center border-b border-border px-4 py-3 sm:px-5">
        <SearchField
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('library.searchApps')}
          className="w-full sm:w-[240px]"
        />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {searching ? (
          searchResults.length === 0 ? (
            <div className="m-4 rounded-xl border border-dashed border-border bg-surface-3/60 p-8 text-center text-[13px] text-muted">
              {t('library.apps.empty')}
            </div>
          ) : (
            searchResults.map((item) => (
              <AppLibraryRow key={item.row.dockId} item={item} last={item.row.dockId === lastDockId} {...handlers(item)} />
            ))
          )
        ) : (
          <>
            {localOrder.length > 0 ? (
              <Reorder.Group axis="y" values={localOrder} onReorder={reorder} as="div">
                {localOrder.map((id) => {
                  const item = resolvedById.get(id);
                  if (!item) return null;
                  return (
                    <DockedReorderRow
                      key={id}
                      item={item}
                      last={id === lastDockId}
                      onCommit={commitOrder}
                      {...handlers(item)}
                    />
                  );
                })}
              </Reorder.Group>
            ) : null}
            {undocked.map((r) => {
              const item = resolvedById.get(r.dockId);
              if (!item) return null;
              return (
                <AppLibraryRow
                  key={r.dockId}
                  item={item}
                  last={r.dockId === lastDockId}
                  leading={<span className="size-6 shrink-0" aria-hidden />}
                  {...handlers(item)}
                />
              );
            })}
          </>
        )}
      </div>
    </div>
  );
};
