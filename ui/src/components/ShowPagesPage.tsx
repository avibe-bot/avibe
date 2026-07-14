import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  ExternalLink,
  Link2,
  Loader2,
  Minus,
  Pencil,
  Plus,
  RefreshCw,
  RotateCw,
  TriangleAlert,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import clsx from 'clsx';

import { useDock } from '../context/DockContext';
import { useWindowManager } from '../context/WindowManagerContext';
import { copyTextToClipboard } from '../lib/utils';
import { copyHref, displayLink, type ShowPageLinkInfo } from '../lib/showPageLinks';
import { type ShowPage, type ShowPagesController, type Visibility } from './useShowPages';
import { filterShowPages, type ShowPageFilter } from '../apps/appLibrary';
import { SHARED_ACTION_ZONE } from '../apps/rowLayout';
import { ShowPageAvatarTile } from '../apps/showPageAvatarTile';
import { ShowPageShareIdField } from './workbench/ShowPageShareIdField';
import { SearchField } from './settings/SettingsPrimitives';
import { Button } from './ui/button';
import { Badge } from './ui/badge';
import { Input } from './ui/input';
import { SegmentedRadio, type SegmentedTone } from './ui/segmented';

const LABEL = 'font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted';

// Visibility → badge variant, active segmented tone, and status dot. The row
// tile is now a letter avatar (hashed by session), so visibility reads from the
// badge rather than a per-state icon.
const STATUS: Record<Visibility, { badge: 'warning' | 'info' | 'secondary'; tone: SegmentedTone; dot: string }> = {
  public: { badge: 'warning', tone: 'gold', dot: 'bg-gold' },
  private: { badge: 'info', tone: 'cyan', dot: 'bg-cyan' },
  offline: { badge: 'secondary', tone: 'muted', dot: 'bg-muted' },
};

interface RowProps {
  page: ShowPage;
  now: number;
  expanded: boolean;
  busy: boolean;
  copied: boolean;
  installed: boolean;
  onOpen: () => void;
  onToggleExpand: () => void;
  onToggleInstall: (next: boolean) => void;
  onRename: (title: string | null) => Promise<void>;
  onSetVisibility: (visibility: Visibility) => void;
  onRotate: () => void;
  onCopy: () => void;
  onShareIdSaved: (payload: ShowPageLinkInfo) => void;
}

function ShowPageRow({
  page,
  now,
  expanded,
  busy,
  copied,
  installed,
  onOpen,
  onToggleExpand,
  onToggleInstall,
  onRename,
  onSetVisibility,
  onRotate,
  onCopy,
  onShareIdSaved,
}: RowProps) {
  const { t, i18n } = useTranslation();
  const status = STATUS[page.visibility];
  const label = page.title || page.session_id;
  const sub = [page.platform ? t(`platform.${page.platform}.title`, { defaultValue: page.platform }) : null, page.agent]
    .filter(Boolean)
    .join(' · ');

  const relative = (iso: string): string => {
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return '';
    const seconds = Math.round((then - now) / 1000);
    const rtf = new Intl.RelativeTimeFormat(i18n.language, { numeric: 'auto' });
    const abs = Math.abs(seconds);
    if (abs < 60) return rtf.format(Math.round(seconds), 'second');
    if (abs < 3600) return rtf.format(Math.round(seconds / 60), 'minute');
    if (abs < 86400) return rtf.format(Math.round(seconds / 3600), 'hour');
    if (abs < 7 * 86400) return rtf.format(Math.round(seconds / 86400), 'day');
    return new Date(iso).toLocaleDateString(i18n.language, { month: 'short', day: 'numeric' });
  };
  const absolute = (iso: string): string => {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(i18n.language, { dateStyle: 'medium', timeStyle: 'short' });
  };

  const href = copyHref(page);
  const shown = displayLink(page);

  return (
    <div className={clsx('border-b border-border last:border-b-0', expanded && 'border-y border-mint/30')}>
      {/* Open-click is limited to the title+icon cluster (§7.1e item 5): the row
          owns the expand panel, so a whole-row click would fight the chevron. The
          rest of the row is inert except its explicit controls. */}
      <div
        className={clsx(
          'flex w-full items-center gap-3 px-4 py-3 sm:gap-4 sm:px-5',
          expanded && 'bg-surface-2',
        )}
      >
        {/* flex-1 lives on this inert wrapper; the open button sizes to its
            content (avatar+title+icon cluster) so the blank space to its right
            does NOT open the app — only the cluster does (§7.1e item 5). The
            cluster carries a hover affordance so openability stays discoverable. */}
        <div className="flex min-w-0 flex-1">
          <button
            type="button"
            onClick={onOpen}
            title={t('showPages.openApp')}
            className="group flex min-w-0 cursor-pointer items-center gap-3 rounded-lg text-left transition-colors hover:bg-foreground/[0.03]"
          >
            <ShowPageAvatarTile sessionId={page.session_id} title={page.title || ''} iconVersion={page.icon_version} />
            <span className="min-w-0">
              <span className="flex items-center gap-1.5">
                <span
                  className={clsx(
                    'truncate text-[13px] font-semibold text-foreground transition-colors group-hover:text-cyan',
                    !page.title && 'font-mono',
                  )}
                >
                  {label}
                </span>
                {/* Open affordance beside the title — a diagonal ↗ (§7.1h item 2). */}
                <ArrowUpRight
                  size={13}
                  aria-hidden
                  className="shrink-0 text-muted/60 transition-colors group-hover:text-cyan"
                />
              </span>
              {sub ? <span className="block truncate text-[11px] text-muted">{sub}</span> : null}
            </span>
          </button>
        </div>

        {/* Status badge — immediately left of the fixed-width action zone, so its
            right-edge column matches the Apps view exactly (§7.1h item 1). */}
        <Badge variant={status.badge} className="hidden sm:inline-flex">
          <span className={clsx('size-1.5 rounded-full', status.dot)} />
          {t(`showPages.status.${page.visibility}`)}
        </Badge>

        {/* Fixed-width, right-justified action zone (shared with the Apps view) so
            the badge column above never shifts with the toggle label width. */}
        <div className={SHARED_ACTION_ZONE}>
          {/* Install toggle — adds the page to the Apps list (and docks it) or
              removes it from both. A sibling of the open button now, so no
              propagation guard is needed. */}
          <Button
            type="button"
            variant={installed ? 'secondary' : 'accent'}
            size="xs"
            onClick={() => onToggleInstall(!installed)}
          >
            {installed ? <Minus /> : <Plus />}
            <span className="hidden sm:inline">{installed ? t('library.apps.remove') : t('library.ai.add')}</span>
          </Button>

          {/* Explicit expand affordance for the management panel. */}
          <button
            type="button"
            aria-label={t('showPages.details')}
            aria-expanded={expanded}
            onClick={() => onToggleExpand()}
            className="grid size-8 shrink-0 place-items-center rounded-lg text-muted transition-colors hover:bg-foreground/[0.05] hover:text-foreground"
          >
            {expanded ? <ChevronUp size={18} className="text-foreground" /> : <ChevronDown size={18} />}
          </button>
        </div>
      </div>

      {expanded ? (
        <div className="bg-surface-2 px-5 pb-6 pt-2">
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_300px]">
            <div className="flex flex-col gap-5">
              <InlineTitleRename page={page} disabled={busy} onRename={onRename} />

              <div className="flex flex-col gap-2">
                <span className={LABEL}>{t('showPages.visibilityLabel')}</span>
                <div className="max-w-[360px]">
                  <SegmentedRadio<Visibility>
                    value={page.visibility}
                    tone={status.tone}
                    disabled={busy}
                    ariaLabel={t('showPages.visibilityLabel')}
                    onChange={onSetVisibility}
                    options={[
                      { id: 'private', label: t('showPages.status.private') },
                      { id: 'public', label: t('showPages.status.public') },
                      { id: 'offline', label: t('showPages.visibilityOffline') },
                    ]}
                  />
                </div>
              </div>

              {page.visibility === 'offline' ? (
                <p className="text-[12px] text-muted">{t('showPages.offlineNoLink')}</p>
              ) : (
                <div className="flex flex-col gap-2">
                  <span className={LABEL}>{t('showPages.liveLink')}</span>
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-border bg-foreground/[0.03] px-3 py-2">
                      <Link2 size={14} className={page.visibility === 'public' ? 'text-gold' : 'text-cyan'} />
                      <span className="truncate font-mono text-[12px] text-foreground">{shown}</span>
                    </div>
                    <Button type="button" variant="secondary" size="sm" onClick={onCopy} disabled={!href}>
                      {copied ? <Check size={14} /> : <Copy size={14} />}
                      {copied ? t('showPages.copied') : t('showPages.copy')}
                    </Button>
                    <Button
                      type="button"
                      variant="secondary"
                      size="sm"
                      disabled={!href}
                      onClick={() => href && window.open(href, '_blank', 'noopener')}
                    >
                      <ExternalLink size={14} />
                      {t('showPages.open')}
                    </Button>
                  </div>
                  {page.visibility === 'public' && !page.url_available ? (
                    <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
                      <TriangleAlert size={13} className="text-gold" />
                      <span className="text-muted">{t('showPages.cloudOff')}</span>
                      <a href="/admin/remote-access" className="font-semibold text-gold hover:underline">
                        {t('showPages.connectCloud')} →
                      </a>
                    </div>
                  ) : null}
                </div>
              )}

              {page.visibility === 'public' ? (
                <div className="flex flex-col gap-3">
                  <div className="flex flex-col gap-2">
                    <span className={LABEL}>{t('showPages.shareId.label')}</span>
                    <div className="max-w-[360px]">
                      <ShowPageShareIdField
                        sessionId={page.session_id}
                        shareId={page.share_id}
                        disabled={busy}
                        onSaved={onShareIdSaved}
                      />
                    </div>
                  </div>
                  <div className="flex flex-col gap-2">
                    <span className={LABEL}>{t('showPages.shareLink')}</span>
                    <div className="flex flex-wrap items-center gap-3">
                      <Button type="button" variant="secondary" size="sm" onClick={onRotate} disabled={busy}>
                        <RotateCw size={14} />
                        {t('showPages.rotate')}
                      </Button>
                      <span className="text-[11px] text-muted">{t('showPages.rotateHint')}</span>
                    </div>
                  </div>
                </div>
              ) : null}
            </div>

            <div className="flex flex-col gap-3 rounded-xl border border-border bg-foreground/[0.02] p-4">
              <span className={LABEL}>{t('showPages.details')}</span>
              {([
                { k: t('showPages.detail.session'), v: page.session_id, mono: true, to: `/chat/${page.session_id}` },
                { k: t('showPages.detail.workspace'), v: page.path, mono: true },
                { k: t('showPages.detail.created'), v: absolute(page.created_at), mono: false },
                { k: t('showPages.detail.updated'), v: relative(page.updated_at), mono: false },
              ] as Array<{ k: string; v: string; mono: boolean; to?: string }>).map((row) => (
                <div key={row.k} className="flex flex-col gap-1">
                  <span className={LABEL}>{row.k}</span>
                  {row.to ? (
                    <Link
                      to={row.to}
                      className={clsx('break-all text-[12px] text-cyan transition-colors hover:text-foreground hover:underline', row.mono && 'font-mono')}
                    >
                      {row.v}
                    </Link>
                  ) : (
                    <span className={clsx('break-all text-[12px] text-foreground', row.mono && 'font-mono')}>{row.v}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

const InlineTitleRename: React.FC<{
  page: ShowPage;
  disabled: boolean;
  onRename: (title: string | null) => Promise<void>;
}> = ({ page, disabled, onRename }) => {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(page.title ?? '');
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const editingRef = useRef(false);

  useEffect(() => {
    if (!editing) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [editing]);

  const start = () => {
    if (disabled || saving) return;
    setDraft(page.title ?? '');
    editingRef.current = true;
    setEditing(true);
  };

  const cancel = () => {
    editingRef.current = false;
    setDraft(page.title ?? '');
    setEditing(false);
  };

  const commit = () => {
    if (!editingRef.current) return;
    editingRef.current = false;
    setEditing(false);
    const next = draft.trim() || null;
    if (next === page.title) return;
    setSaving(true);
    void onRename(next)
      .catch(() => undefined)
      .finally(() => setSaving(false));
  };

  return (
    <div className="flex flex-col gap-2">
      <span className={LABEL}>{t('showPages.nameLabel')}</span>
      {editing ? (
        <Input
          ref={inputRef}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={commit}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              commit();
            } else if (event.key === 'Escape') {
              event.preventDefault();
              event.stopPropagation();
              cancel();
            }
          }}
          placeholder={t('chat.titlePlaceholder')}
          aria-label={t('showPages.nameLabel')}
          className="h-9 max-w-[360px] px-3 text-[13px]"
        />
      ) : (
        <div className="flex max-w-[360px] items-center gap-2 rounded-lg border border-border bg-foreground/[0.03] px-3 py-2">
          <span className="min-w-0 flex-1 truncate text-[13px] text-foreground">
            {page.title?.trim() || t('chat.untitled')}
          </span>
          <button
            type="button"
            onClick={start}
            disabled={disabled || saving}
            title={t('showPages.rename')}
            aria-label={t('showPages.rename')}
            className="grid size-6 shrink-0 place-items-center rounded-md text-muted transition-colors hover:bg-foreground/[0.06] hover:text-foreground disabled:opacity-50"
          >
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Pencil className="size-3.5" />}
          </button>
        </div>
      )}
    </div>
  );
};

// The full Show Pages inventory view (the "AI" tab): search + visibility filter +
// rows that OPEN the page as an app window on click, each with a state-aware
// install toggle (添加到 App ↔ 移出) and an explicit chevron for the share-link
// management panel. Shared by the App Library window and the mobile full-screen
// route; the caller owns the pages state via useShowPages so both projections stay
// consistent, and passes `onOpenApp` so opening reuses the showpage window.
export function ShowPagesView({
  pages,
  busyId,
  setVisibility,
  rotate,
  rename,
  onShareIdSaved,
  reload,
  onOpenApp,
}: ShowPagesController & { onOpenApp?: (sessionId: string, title?: string) => void }) {
  const { t } = useTranslation();
  const { isPinned, pin, unpin } = useDock();
  const { windows, setParams, setTitle } = useWindowManager();
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<ShowPageFilter>('all');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  const copy = async (page: ShowPage) => {
    const href = copyHref(page);
    if (!href) return;
    await copyTextToClipboard(href);
    setCopiedId(page.session_id);
    window.setTimeout(() => setCopiedId((id) => (id === page.session_id ? null : id)), 1600);
  };

  const visible = useMemo(() => filterShowPages(pages, filter, search), [pages, filter, search]);

  // The inventory is the title source for this view. Project it into matching
  // windows so optimistic edits, rollbacks, and newer session.activity events
  // all take the same ordered path instead of racing independent callbacks.
  useEffect(() => {
    pages.forEach((page) => {
      const title = page.title?.trim() || t('chat.untitled');
      windows
        .filter((win) => win.appId === 'showpage' && win.params?.sessionId === page.session_id)
        .forEach((win) => {
          if (win.title !== title) setTitle(win.id, title);
          if (win.params?.title !== title) setParams(win.id, { title });
        });
    });
  }, [pages, setParams, setTitle, t, windows]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3 sm:px-5">
        <div className="flex items-center gap-2">
          <SearchField
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('library.searchPages')}
            className="w-full sm:w-[240px]"
          />
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={reload}
            title={t('common.refresh', { defaultValue: 'Refresh' })}
            className="px-2.5"
          >
            <RefreshCw size={14} />
          </Button>
        </div>
        <SegmentedRadio<ShowPageFilter>
          value={filter}
          onChange={setFilter}
          ariaLabel={t('showPages.filterAria')}
          options={[
            { id: 'all', label: t('showPages.filter.all') },
            { id: 'public', label: t('showPages.filter.public') },
            { id: 'private', label: t('showPages.filter.private') },
            { id: 'offline', label: t('showPages.filter.offline') },
          ]}
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {visible.length === 0 ? (
          <div className="m-4 rounded-xl border border-dashed border-border bg-surface-3/60 p-8 text-center text-[13px] text-muted">
            {pages.length === 0 ? t('showPages.empty') : t('showPages.emptyFiltered')}
          </div>
        ) : (
          visible.map((page) => (
            <ShowPageRow
              key={page.session_id}
              page={page}
              now={now}
              expanded={expandedId === page.session_id}
              busy={busyId === page.session_id}
              copied={copiedId === page.session_id}
              installed={isPinned(page.session_id)}
              onOpen={() => onOpenApp?.(page.session_id, page.title ?? undefined)}
              onToggleExpand={() => setExpandedId((id) => (id === page.session_id ? null : page.session_id))}
              onToggleInstall={(next) => (next ? pin(page.session_id) : unpin(page.session_id))}
              onRename={(title) => rename(page, title)}
              onSetVisibility={(visibility) => setVisibility(page, visibility)}
              onRotate={() => rotate(page)}
              onCopy={() => copy(page)}
              onShareIdSaved={onShareIdSaved}
            />
          ))
        )}
      </div>
    </div>
  );
}
