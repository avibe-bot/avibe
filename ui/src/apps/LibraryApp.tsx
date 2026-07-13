import { useMemo, useState } from 'react';
import { Lock, PinOff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

import { APP_LIST, APP_REGISTRY, type AppDefinition, type AppId } from './registry';
import { deriveAppRows, type AppRow } from './appLibrary';
import { ShowPageAvatarTile } from './showPageAvatarTile';
import { useDock } from '../context/DockContext';
import { ShowPagesView } from '../components/ShowPagesPage';
import { useShowPages, type ShowPage } from '../components/useShowPages';
import { Badge } from '../components/ui/badge';
import { SearchField } from '../components/settings/SettingsPrimitives';

// The App Library: the app manager, itself a built-in app (§7.1). Two views over
// the same data — the docked set (Apps) and the full Show Pages inventory — with
// ONE state bit between them: being an app ≡ being in the Dock. The Show Pages
// view's per-row Dock switch is the single promote/demote gesture; the Apps view
// only lists + removes. Renders as a window body (desktop) and as a full-screen
// route (mobile), so `windowId`/`params` are accepted but unused.

// The Library never lists itself among the apps it manages.
const LIBRARY_APP_ID: AppId = 'library';
// The client's built-in Dock ids (files / terminal / editor / library), the set
// deriveAppRows classifies against. Mirrors DockContext's BUILTIN_DOCK_IDS.
const BUILTIN_IDS = new Set<string>(APP_LIST.map((app) => app.id));

type LibraryTab = 'apps' | 'showpages';

export const LibraryApp: React.FC<{ windowId?: string; params?: Record<string, unknown>; initialTab?: LibraryTab }> = ({
  params,
  initialTab,
}) => {
  const { t } = useTranslation();
  const controller = useShowPages();
  const { order } = useDock();
  // The legacy /admin/show-pages redirect (mobile via prop, desktop window via
  // params) can request the Show Pages tab up front; default to Apps otherwise.
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

  const appsCount = useMemo(() => deriveAppRows(order, BUILTIN_IDS, LIBRARY_APP_ID).length, [order]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-surface text-foreground">
      <div className="flex shrink-0 items-center gap-1 border-b border-border px-3 py-2.5 sm:px-4">
        <TabButton active={tab === 'apps'} onClick={() => setTab('apps')} label={t('library.tab.apps')} count={appsCount} />
        <TabButton
          active={tab === 'showpages'}
          onClick={() => setTab('showpages')}
          label={t('library.tab.showPages')}
          count={controller.pages.length}
        />
      </div>
      <div className="min-h-0 flex-1">
        {tab === 'apps' ? <AppsView pages={controller.pages} /> : <ShowPagesView {...controller} />}
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

// Apps view: the docked set, in order, minus the Library itself. Built-ins show
// their icon + a locked badge (not removable); pinned Show Pages show a letter
// avatar + a remove-from-Dock action that unpins without deleting the page. No
// per-row toggles, no reorder (that lives on the Dock), no Add App (Phase 3).
const AppsView: React.FC<{ pages: ShowPage[] }> = ({ pages }) => {
  const { t } = useTranslation();
  const { order, pins, unpin } = useDock();
  const [query, setQuery] = useState('');

  const pinBySession = useMemo(() => new Map(pins.map((p) => [p.session_id, p])), [pins]);
  const pageBySession = useMemo(() => new Map(pages.map((p) => [p.session_id, p])), [pages]);
  const rows = useMemo(() => deriveAppRows(order, BUILTIN_IDS, LIBRARY_APP_ID), [order]);

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
                className="flex items-center gap-3 border-b border-border px-4 py-3 last:border-b-0 sm:gap-4 sm:px-5"
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
                  <Badge variant="outline" className="font-mono text-[10px] uppercase tracking-wide">
                    {t('library.kind.builtin')}
                  </Badge>
                ) : (
                  <Badge variant="success">{t('library.kind.showPage')}</Badge>
                )}
                {row.removable ? (
                  <button
                    type="button"
                    title={t('library.apps.remove')}
                    aria-label={t('library.apps.remove')}
                    onClick={() => row.sessionId && unpin(row.sessionId)}
                    className="grid size-8 shrink-0 place-items-center rounded-lg text-muted transition-colors hover:bg-destructive/10 hover:text-destructive"
                  >
                    <PinOff size={15} />
                  </button>
                ) : (
                  <span
                    className="grid size-8 shrink-0 place-items-center text-muted/60"
                    title={t('library.apps.locked')}
                    aria-label={t('library.apps.locked')}
                  >
                    <Lock size={14} />
                  </span>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};
