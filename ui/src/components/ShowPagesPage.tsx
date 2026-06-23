import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Check,
  ChevronDown,
  ChevronUp,
  CloudOff,
  Copy,
  ExternalLink,
  Globe,
  Link2,
  Lock,
  RefreshCw,
  RotateCw,
  TriangleAlert,
  type LucideIcon,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import clsx from 'clsx';

import { useApi } from '../context/ApiContext';
import { useToast } from '../context/ToastContext';
import { copyTextToClipboard } from '../lib/utils';
import { copyHref, displayLink, type ShowPageLinkInfo } from '../lib/showPageLinks';
import { ShowPageShareIdField } from './workbench/ShowPageShareIdField';
import { SearchField } from './settings/SettingsPrimitives';
import { Button } from './ui/button';
import { Badge } from './ui/badge';
import { SegmentedRadio, type SegmentedTone } from './ui/segmented';

type Visibility = 'private' | 'public' | 'offline';
type Filter = 'online' | Visibility;

interface ShowPage {
  session_id: string;
  visibility: Visibility;
  title: string | null;
  platform: string | null;
  agent: string | null;
  path: string;
  active_url: string | null;
  private_url: string | null;
  public_url: string | null;
  url_available: boolean;
  share_id: string | null;
  offline: boolean;
  offline_at: string | null;
  created_at: string;
  updated_at: string;
}

const LABEL = 'font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted';

const STATUS: Record<
  Visibility,
  { Icon: LucideIcon; badge: 'warning' | 'info' | 'secondary'; tone: SegmentedTone; dot: string; tile: string; iconColor: string }
> = {
  public: { Icon: Globe, badge: 'warning', tone: 'gold', dot: 'bg-gold', tile: 'border-gold/40 bg-gold/10', iconColor: 'text-gold' },
  private: { Icon: Lock, badge: 'info', tone: 'cyan', dot: 'bg-cyan', tile: 'border-cyan/40 bg-cyan-soft', iconColor: 'text-cyan' },
  offline: { Icon: CloudOff, badge: 'secondary', tone: 'muted', dot: 'bg-muted', tile: 'border-border-strong bg-foreground/[0.04]', iconColor: 'text-muted' },
};


interface RowProps {
  page: ShowPage;
  expanded: boolean;
  busy: boolean;
  copied: boolean;
  onToggle: () => void;
  onSetVisibility: (visibility: Visibility) => void;
  onRotate: () => void;
  onCopy: () => void;
  onShareIdSaved: (payload: ShowPageLinkInfo) => void;
}

function ShowPageRow({ page, expanded, busy, copied, onToggle, onSetVisibility, onRotate, onCopy, onShareIdSaved }: RowProps) {
  const { t, i18n } = useTranslation();
  const status = STATUS[page.visibility];
  const { Icon } = status;
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
          // Only the row itself toggles via keyboard; let Enter/Space on nested
          // interactives (the live-link anchor) activate them normally instead
          // of being swallowed by the row toggle.
          if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            onToggle();
          }
        }}
        className={clsx(
          'flex w-full cursor-pointer items-center gap-4 px-6 py-3.5 text-left transition-colors',
          expanded ? 'bg-surface-2' : 'hover:bg-foreground/[0.02]'
        )}
      >
        <span className="flex min-w-0 flex-1 items-center gap-3">
          <span className={clsx('flex size-8 shrink-0 items-center justify-center rounded-lg border', status.tile)}>
            <Icon size={16} className={status.iconColor} />
          </span>
          <span className="min-w-0">
            <span className={clsx('block truncate text-[13px] font-semibold text-foreground', !page.title && 'font-mono')}>
              {label}
            </span>
            {sub ? <span className="block truncate text-[11px] text-muted">{sub}</span> : null}
          </span>
        </span>

        <span className="w-[120px] shrink-0">
          <Badge variant={status.badge}>
            <span className={clsx('size-1.5 rounded-full', status.dot)} />
            {t(`showPages.status.${page.visibility}`)}
          </Badge>
        </span>

        <span className="hidden w-[280px] shrink-0 items-center gap-1.5 lg:flex">
          {shown && href ? (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              title={href}
              className="flex min-w-0 items-center gap-1 font-mono text-[12px] text-muted transition-colors hover:text-foreground hover:underline"
            >
              <span className="truncate">{shown}</span>
              <ExternalLink size={12} className="shrink-0" />
            </a>
          ) : shown ? (
            <span className="truncate font-mono text-[12px] text-muted">{shown}</span>
          ) : (
            <span className="text-[13px] text-muted">—</span>
          )}
        </span>

        <span className="hidden w-[96px] shrink-0 text-[12px] text-muted sm:block">{relative(page.updated_at)}</span>

        <span className="flex w-[24px] shrink-0 justify-end">
          {expanded ? <ChevronUp size={18} className="text-foreground" /> : <ChevronDown size={18} className="text-muted" />}
        </span>
      </div>

      {expanded ? (
        <div className="bg-surface-2 px-6 pb-6 pt-2">
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

export function ShowPagesPage() {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [pages, setPages] = useState<ShowPage[]>([]);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<Filter>('online');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  const load = useCallback(() => {
    api
      .getShowPages()
      .then((res: any) => setPages(Array.isArray(res?.pages) ? res.pages : []))
      .catch(() => {});
  }, [api]);

  useEffect(() => {
    load();
  }, [load, refreshTrigger]);

  const mergePage = (next: ShowPage) =>
    setPages((prev) => prev.map((page) => (page.session_id === next.session_id ? { ...page, ...next } : page)));

  const setVisibility = async (page: ShowPage, visibility: Visibility) => {
    if (page.visibility === visibility || busyId) return;
    setBusyId(page.session_id);
    try {
      const res = await api.setShowPageVisibility(page.session_id, visibility);
      mergePage(res);
      showToast(t('showPages.toast.updated'));
    } catch {
      // ApiContext surfaces a toast on failure.
    } finally {
      setBusyId(null);
    }
  };

  const rotate = async (page: ShowPage) => {
    if (busyId) return;
    setBusyId(page.session_id);
    try {
      const res = await api.rotateShowPageShare(page.session_id);
      mergePage(res);
      showToast(t('showPages.toast.rotated'));
    } catch {
      // handled by ApiContext
    } finally {
      setBusyId(null);
    }
  };

  // The custom-link field owns its own request/validation; the page only merges
  // the returned payload (new share_id, updated_at) and confirms.
  const onShareIdSaved = (next: ShowPageLinkInfo) => {
    mergePage(next as ShowPage);
    showToast(t('showPages.shareId.toast.saved'));
  };

  const copy = async (page: ShowPage) => {
    const href = copyHref(page);
    if (!href) return;
    await copyTextToClipboard(href);
    setCopiedId(page.session_id);
    window.setTimeout(() => setCopiedId((id) => (id === page.session_id ? null : id)), 1600);
  };

  const visible = useMemo(() => {
    const query = search.trim().toLowerCase();
    return pages.filter((page) => {
      // "online" = live pages (private + public), i.e. everything but offline.
      if (filter === 'online' ? page.visibility === 'offline' : page.visibility !== filter) return false;
      if (!query) return true;
      return (page.title || '').toLowerCase().includes(query) || page.session_id.toLowerCase().includes(query);
    });
  }, [pages, filter, search]);

  return (
    <div className="flex h-full flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-col gap-1.5">
          <h1 className="text-[28px] font-bold leading-tight tracking-[-0.4px] text-foreground">{t('showPages.title')}</h1>
          <p className="text-[14px] leading-[1.55] text-muted">{t('showPages.subtitle')}</p>
        </div>
        <div className="flex items-center gap-2">
          <SearchField
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('showPages.searchPlaceholder')}
            className="w-full sm:w-[280px]"
          />
          <Button
            type="button"
            variant="secondary"
            size="sm"
            onClick={() => setRefreshTrigger((v) => v + 1)}
            title={t('common.refresh', { defaultValue: 'Refresh' })}
            className="px-3"
          >
            <RefreshCw size={14} />
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-3 px-1 py-2">
        <div className="flex items-center gap-2">
          <span className="text-[15px] font-semibold text-foreground">{t('showPages.allPages')}</span>
          <span className="rounded-full bg-foreground/[0.08] px-1.5 py-0.5 font-mono text-[11px] font-bold text-muted">
            {pages.length}
          </span>
          <span className="text-[12px] text-muted">· {t('showPages.sortedRecent')}</span>
        </div>
        <SegmentedRadio<Filter>
          value={filter}
          onChange={setFilter}
          ariaLabel={t('showPages.filterAria')}
          options={[
            { id: 'online', label: t('showPages.filter.online') },
            { id: 'private', label: t('showPages.filter.private') },
            { id: 'public', label: t('showPages.filter.public') },
            { id: 'offline', label: t('showPages.filter.offline') },
          ]}
        />
      </div>

      <div className="flex-1 overflow-y-auto">
        {visible.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface-3/60 p-8 text-center text-[13px] text-muted">
            {pages.length === 0 ? t('showPages.empty') : t('showPages.emptyFiltered')}
          </div>
        ) : (
          <div className="overflow-hidden rounded-xl border border-border bg-background">
            <div className="flex items-center gap-4 border-b border-border bg-surface px-6 py-3">
              <span className={clsx(LABEL, 'flex-1')}>{t('showPages.col.session')}</span>
              <span className={clsx(LABEL, 'w-[120px] shrink-0')}>{t('showPages.col.visibility')}</span>
              <span className={clsx(LABEL, 'hidden w-[280px] shrink-0 lg:block')}>{t('showPages.col.link')}</span>
              <span className={clsx(LABEL, 'hidden w-[96px] shrink-0 sm:block')}>{t('showPages.col.updated')}</span>
              <span className="w-[24px] shrink-0" />
            </div>
            {visible.map((page) => (
              <ShowPageRow
                key={page.session_id}
                page={page}
                expanded={expandedId === page.session_id}
                busy={busyId === page.session_id}
                copied={copiedId === page.session_id}
                onToggle={() => setExpandedId((id) => (id === page.session_id ? null : page.session_id))}
                onSetVisibility={(visibility) => setVisibility(page, visibility)}
                onRotate={() => rotate(page)}
                onCopy={() => copy(page)}
                onShareIdSaved={onShareIdSaved}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
