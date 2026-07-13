import { useMemo, useState } from 'react';
import {
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  ExternalLink,
  Link2,
  RefreshCw,
  RotateCw,
  TriangleAlert,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import clsx from 'clsx';

import { useDock } from '../context/DockContext';
import { copyTextToClipboard } from '../lib/utils';
import { copyHref, displayLink, type ShowPageLinkInfo } from '../lib/showPageLinks';
import { type ShowPage, type ShowPagesController, type Visibility } from './useShowPages';
import { filterShowPages, type ShowPageFilter } from '../apps/appLibrary';
import { ShowPageAvatarTile } from '../apps/showPageAvatarTile';
import { ShowPageShareIdField } from './workbench/ShowPageShareIdField';
import { SearchField } from './settings/SettingsPrimitives';
import { Button } from './ui/button';
import { Badge } from './ui/badge';
import { Switch } from './ui/switch';
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
  expanded: boolean;
  busy: boolean;
  copied: boolean;
  pinned: boolean;
  onToggle: () => void;
  onTogglePin: (next: boolean) => void;
  onSetVisibility: (visibility: Visibility) => void;
  onRotate: () => void;
  onCopy: () => void;
  onShareIdSaved: (payload: ShowPageLinkInfo) => void;
}

function ShowPageRow({
  page,
  expanded,
  busy,
  copied,
  pinned,
  onToggle,
  onTogglePin,
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
    const seconds = Math.round((then - Date.now()) / 1000);
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
      <div
        role="button"
        tabIndex={0}
        onClick={onToggle}
        onKeyDown={(e) => {
          if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            onToggle();
          }
        }}
        className={clsx(
          'flex w-full cursor-pointer items-center gap-3 px-4 py-3 text-left transition-colors sm:gap-4 sm:px-5',
          expanded ? 'bg-surface-2' : 'hover:bg-foreground/[0.02]',
        )}
      >
        <span className="flex min-w-0 flex-1 items-center gap-3">
          <ShowPageAvatarTile sessionId={page.session_id} title={page.title || ''} />
          <span className="min-w-0">
            <span className={clsx('block truncate text-[13px] font-semibold text-foreground', !page.title && 'font-mono')}>
              {label}
            </span>
            {sub ? <span className="block truncate text-[11px] text-muted">{sub}</span> : null}
          </span>
        </span>

        <Badge variant={status.badge} className="hidden sm:inline-flex">
          <span className={clsx('size-1.5 rounded-full', status.dot)} />
          {t(`showPages.status.${page.visibility}`)}
        </Badge>

        {/* The single promote/demote gesture: the Dock switch pins (installs) or
            unpins the page. Stop propagation so toggling it never expands the row. */}
        <span className="flex shrink-0 flex-col items-center gap-1" onClick={(e) => e.stopPropagation()}>
          <Switch checked={pinned} onCheckedChange={onTogglePin} label={t('library.dock.toggle')} />
          <span className="font-mono text-[9px] font-bold uppercase tracking-[0.1em] text-muted">{t('library.dock.label')}</span>
        </span>

        <button
          type="button"
          title={t('showPages.open')}
          disabled={!href}
          onClick={(e) => {
            e.stopPropagation();
            if (href) window.open(href, '_blank', 'noopener,noreferrer');
          }}
          className="grid size-8 shrink-0 place-items-center rounded-lg text-muted transition-colors hover:bg-foreground/[0.05] hover:text-foreground disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-muted"
        >
          <ExternalLink size={15} />
        </button>

        <span className="flex w-[20px] shrink-0 justify-end">
          {expanded ? <ChevronUp size={18} className="text-foreground" /> : <ChevronDown size={18} className="text-muted" />}
        </span>
      </div>

      {expanded ? (
        <div className="bg-surface-2 px-5 pb-6 pt-2">
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_300px]">
            <div className="flex flex-col gap-5">
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

// The full Show Pages inventory view: search + visibility filter + expandable
// rows, each with the Dock switch that pins/unpins the page. Shared by the App
// Library window and the mobile full-screen route; the caller owns the pages
// state via useShowPages so both projections stay consistent.
export function ShowPagesView({ pages, busyId, setVisibility, rotate, onShareIdSaved, reload }: ShowPagesController) {
  const { t } = useTranslation();
  const { isPinned, pin, unpin } = useDock();
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<ShowPageFilter>('all');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  const copy = async (page: ShowPage) => {
    const href = copyHref(page);
    if (!href) return;
    await copyTextToClipboard(href);
    setCopiedId(page.session_id);
    window.setTimeout(() => setCopiedId((id) => (id === page.session_id ? null : id)), 1600);
  };

  const visible = useMemo(() => filterShowPages(pages, filter, search), [pages, filter, search]);

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
              expanded={expandedId === page.session_id}
              busy={busyId === page.session_id}
              copied={copiedId === page.session_id}
              pinned={isPinned(page.session_id)}
              onToggle={() => setExpandedId((id) => (id === page.session_id ? null : page.session_id))}
              onTogglePin={(next) => (next ? pin(page.session_id) : unpin(page.session_id))}
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
