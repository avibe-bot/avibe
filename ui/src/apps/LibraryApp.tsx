import { useCallback, useMemo, useState } from 'react';
import { Pin, PinOff, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import clsx from 'clsx';

import { APP_LIST, APP_REGISTRY, type AppDefinition, type AppId } from './registry';
import { deriveAppRows, type AppRow } from './appLibrary';
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
}

// Apps view: the INSTALLED set (built-ins + pinned Show Pages), in canonical
// order. Every row opens the app on click and carries a state-aware Dock toggle
// (固定到 Dock ↔ 取消固定) that never removes it from the list; Show Page rows add a
// 移出 that uninstalls the app (the page itself is untouched). Built-ins have no
// 移出 and no lock — they simply can't leave the list.
const AppsView: React.FC<{ pages: ShowPage[]; openApp: ReturnType<typeof useOpenApp> }> = ({ pages, openApp }) => {
  const { t } = useTranslation();
  const { order, pins, dock, undock, unpin } = useDock();
  const [query, setQuery] = useState('');

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
        return { row, name, subtitle };
      }),
    [rows, pageBySession, pinBySession, t],
  );

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? resolved.filter((item) => item.name.toLowerCase().includes(q)) : resolved;
  }, [resolved, query]);

  const openRow = (row: AppRow) =>
    row.kind === 'builtin'
      ? openApp(row.builtinId as AppId)
      : openApp('showpage', {
          sessionId: row.sessionId ?? '',
          title: pageBySession.get(row.sessionId ?? '')?.title ?? undefined,
        });

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
        {filtered.length === 0 ? (
          <div className="m-4 rounded-xl border border-dashed border-border bg-surface-3/60 p-8 text-center text-[13px] text-muted">
            {t('library.apps.empty')}
          </div>
        ) : (
          filtered.map(({ row, name, subtitle, def }) => {
            const Icon = def?.icon;
            return (
              <div
                key={row.dockId}
                role="button"
                tabIndex={0}
                onClick={() => openRow(row)}
                onKeyDown={(e) => {
                  if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
                    e.preventDefault();
                    openRow(row);
                  }
                }}
                className="flex w-full cursor-pointer items-center gap-3 border-b border-border px-4 py-3 text-left transition-colors last:border-b-0 hover:bg-foreground/[0.02] sm:gap-4 sm:px-5"
              >
                {def && Icon ? (
                  <span
                    className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-border"
                    style={{ color: `var(${def.accent})`, backgroundColor: `color-mix(in srgb, var(${def.accent}) 14%, transparent)` }}
                  >
                    <Icon className="size-[18px]" />
                  </span>
                ) : (
                  <ShowPageAvatarTile sessionId={row.sessionId ?? ''} title={name} />
                )}
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-[13px] font-semibold text-foreground">{name}</span>
                  {subtitle ? <span className="truncate font-mono text-[11px] text-muted">{subtitle}</span> : null}
                </span>
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
                    void (row.docked ? undock(row.dockId) : dock(row.dockId));
                  }}
                >
                  {row.docked ? <PinOff /> : <Pin />}
                  <span className="hidden sm:inline">{row.docked ? t('library.apps.undock') : t('library.apps.dock')}</span>
                </Button>
                {/* 移出 — uninstall (Show Pages only; the page itself is untouched). */}
                {row.kind === 'showpage' ? (
                  <button
                    type="button"
                    title={t('library.apps.remove')}
                    aria-label={t('library.apps.remove')}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (row.sessionId) void unpin(row.sessionId);
                    }}
                    className="grid size-8 shrink-0 place-items-center rounded-lg text-muted transition-colors hover:bg-destructive/10 hover:text-destructive"
                  >
                    <Trash2 size={15} />
                  </button>
                ) : null}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};
