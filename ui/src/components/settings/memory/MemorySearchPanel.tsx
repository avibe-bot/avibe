import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  Loader2,
  RotateCw,
  Search as SearchIcon,
} from 'lucide-react';

import { Badge } from '../../ui/badge';
import { Button } from '../../ui/button';
import { Card, CardContent } from '../../ui/card';
import { Input } from '../../ui/input';
import { SegmentedRadio } from '../../ui/segmented';
import { Select } from '../../ui/select';
import { useApi } from '../../../context/ApiContext';
import type {
  MemoryListItem,
  MemoryListResult,
  MemoryOrigin,
  MemoryRecallResult,
} from '../../../context/ApiContext';
import { copyTextToClipboard } from '../../../lib/utils';
import { useMemoryResource } from './useMemoryResource';
import { memoryOriginLabelKey } from './memoryOrigin';

type MemoryItemsOk = Extract<MemoryRecallResult, { status: 'ok' }>;
type MemoryListOk = Extract<MemoryListResult, { status: 'ok' }>;

const PAGE_SIZE = 20;
const FALLBACK_PROJECTS = [
  { id: 'default', kind: 'default' as const },
  { id: 'all', kind: 'all' as const },
];

function episodeTitle(item: MemoryListItem): string {
  return item.subject || item.summary || item.body;
}

function episodeIdentity(item: MemoryListItem): string {
  return JSON.stringify([item.origin, item.project, item.id]);
}

function formatEpisodeTimestamp(value: string): string {
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return value;
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${instant.getFullYear()}-${pad(instant.getMonth() + 1)}-${pad(instant.getDate())} · ${pad(instant.getHours())}:${pad(instant.getMinutes())}`;
}

function visiblePageNumbers(current: number, total: number): number[] {
  const width = Math.min(5, total);
  const start = Math.max(1, Math.min(current - 2, total - width + 1));
  return Array.from({ length: width }, (_, index) => start + index);
}

export const MemorySearchPanel: React.FC<{ enabled: boolean }> = ({ enabled }) => {
  const { t } = useTranslation();
  const api = useApi();
  const [query, setQuery] = useState('');
  const [project, setProject] = useState('default');
  const [browseOrigin, setBrowseOrigin] = useState<MemoryOrigin>('user');
  const [projects, setProjects] = useState<Array<{ id: string; kind: string }>>(
    FALLBACK_PROJECTS,
  );
  const [browsePage, setBrowsePage] = useState(1);
  const [cursorByPage, setCursorByPage] = useState<Record<number, string | null>>({ 1: null });
  const browseRequestRef = useRef(0);
  const browsePositionRef = useRef({
    project: 'default',
    origin: 'user' as MemoryOrigin,
    page: 1,
    cursor: null as string | null,
  });
  const [selectedEpisodeKey, setSelectedEpisodeKey] = useState<string | null>(null);
  const [copiedEpisodeKey, setCopiedEpisodeKey] = useState<string | null>(null);
  const browseMode = !query.trim();

  const searchRead = useCallback(
    (text: string, selected: string) => api.searchMemory(text, PAGE_SIZE, selected),
    [api],
  );
  const {
    data: searchData,
    error: searchError,
    loading: searching,
    loaded: searched,
    reload: search,
  } = useMemoryResource<MemoryItemsOk, [string, string]>({
    read: searchRead,
    failureMessageKey: 'memory.search.searchFailed',
    clearErrorOnReload: true,
    resetDataOnError: true,
  });

  const browseRead = useCallback(
    async (
      selected: string,
      origin: MemoryOrigin,
      page: number,
      cursor: string | null,
    ) => {
      const request = ++browseRequestRef.current;
      const result = await api.listMemoryEpisodes(selected, {
        page,
        cursor,
        limit: PAGE_SIZE,
        ...(origin === 'agent' ? { origin } : {}),
      });
      if (
        request === browseRequestRef.current
        && selected === 'all'
        && result.status === 'ok'
      ) {
        setCursorByPage((current) => {
          const next = Object.fromEntries(
            Object.entries(current).filter(([knownPage]) => Number(knownPage) <= page),
          );
          if (result.next_cursor) next[page + 1] = result.next_cursor;
          return next;
        });
      }
      return result;
    },
    [api],
  );
  const {
    data: browseData,
    error: browseError,
    loading: browsing,
    loaded: browsed,
    reload: browse,
  } = useMemoryResource<MemoryListOk, [string, MemoryOrigin, number, string | null]>({
    read: browseRead,
    failureMessageKey: 'memory.search.browse.loadFailed',
    enabled,
    clearErrorOnReload: true,
    resetDataOnError: true,
  });

  useEffect(() => {
    if (!enabled) return;
    void api
      .listMemoryProjects()
      .then((result) => {
        if (result && 'status' in result && result.status === 'ok' && Array.isArray(result.projects)) {
          setProjects(result.projects);
        }
      })
      .catch(() => setProjects(FALLBACK_PROJECTS));
  }, [api, enabled]);

  useEffect(() => {
    if (!enabled || !browseMode) return;
    const position = browsePositionRef.current;
    const sameScope = position.project === project && position.origin === browseOrigin;
    const page = sameScope ? position.page : 1;
    const cursor = sameScope ? position.cursor : null;
    browsePositionRef.current = { project, origin: browseOrigin, page, cursor };
    void browse(project, browseOrigin, page, cursor);
  }, [browse, browseMode, browseOrigin, enabled, project]);

  const resetBrowseNavigation = (
    selectedProject = project,
    selectedOrigin = browseOrigin,
  ) => {
    browseRequestRef.current += 1;
    browsePositionRef.current = {
      project: selectedProject,
      origin: selectedOrigin,
      page: 1,
      cursor: null,
    };
    setCursorByPage({ 1: null });
    setBrowsePage(1);
    setSelectedEpisodeKey(null);
    setCopiedEpisodeKey(null);
  };

  const runSearch = () => {
    const trimmed = query.trim();
    if (!trimmed) return;
    void search(trimmed, project);
  };

  const goToBrowsePage = (nextPage: number) => {
    if (nextPage < 1 || nextPage === browsePage) return;
    const cursor = project === 'all' ? cursorByPage[nextPage] : null;
    if (project === 'all' && cursor === undefined) return;
    browsePositionRef.current = { project, origin: browseOrigin, page: nextPage, cursor };
    setBrowsePage(nextPage);
    setSelectedEpisodeKey(null);
    setCopiedEpisodeKey(null);
    void browse(project, browseOrigin, nextPage, cursor);
  };

  const retryBrowsePage = () => {
    const cursor = project === 'all' ? cursorByPage[browsePage] : null;
    if (project === 'all' && cursor === undefined) return;
    browsePositionRef.current = { project, origin: browseOrigin, page: browsePage, cursor };
    setSelectedEpisodeKey(null);
    setCopiedEpisodeKey(null);
    void browse(project, browseOrigin, browsePage, cursor);
  };

  const searchItems = searchData?.items ?? null;
  const searchWarnings = searchData?.warnings ?? [];
  const browseItems = browseData?.items ?? [];
  const browseIncomplete = browseData?.warnings.some(
    (warning) => warning === 'memory_list_partial' || warning === 'memory_list_truncated',
  ) ?? false;
  const selectedEpisode = selectedEpisodeKey
    ? (browseItems.find((item) => episodeIdentity(item) === selectedEpisodeKey) ?? null)
    : null;
  const knownPageCount = Math.max(1, ...Object.keys(cursorByPage).map(Number));
  const totalPages = browseData?.total_count != null
    ? Math.max(1, Math.ceil(browseData.total_count / PAGE_SIZE))
    : Math.max(knownPageCount, browsePage + (browseData?.next_cursor ? 1 : 0));
  const hasNextBrowsePage = project === 'all'
    ? Boolean(browseData?.next_cursor)
    : browsePage < totalPages;
  const pageNumbers = useMemo(
    () => visiblePageNumbers(browsePage, totalPages),
    [browsePage, totalPages],
  );
  const browsePagination = (
    <nav className="flex flex-wrap items-center justify-center gap-1.5" aria-label={t('memory.search.browse.episodes')}>
      <Button
        variant="secondary"
        size="sm"
        onClick={() => goToBrowsePage(browsePage - 1)}
        disabled={browsePage <= 1 || browsing}
        aria-label={t('memory.search.browse.previous')}
      >
        <ChevronLeft className="size-3.5" />
      </Button>
      {pageNumbers.map((page) => {
        const available = project !== 'all' || cursorByPage[page] !== undefined;
        return (
          <Button
            key={page}
            variant={page === browsePage ? 'default' : 'secondary'}
            size="icon"
            className="text-[13px]"
            onClick={() => goToBrowsePage(page)}
            disabled={!available || browsing}
            aria-current={page === browsePage ? 'page' : undefined}
            aria-label={t('memory.search.browse.page', { page })}
          >
            {page}
          </Button>
        );
      })}
      <Button
        variant="secondary"
        size="sm"
        onClick={() => goToBrowsePage(browsePage + 1)}
        disabled={!hasNextBrowsePage || browsing}
        aria-label={t('memory.search.browse.next')}
      >
        <ChevronRight className="size-3.5" />
      </Button>
    </nav>
  );

  if (!enabled) {
    return (
      <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-5 py-4">
        <p className="text-[13px] font-semibold text-destructive-ink">
          {t('memory.search.browse.closedTitle')}
        </p>
        <p className="mt-1 text-[12.5px] text-muted">
          {t('memory.search.browse.closedDescription')}
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <p className="text-[12.5px] text-muted">{t('memory.search.description')}</p>
      <div className="flex flex-col gap-2 lg:flex-row">
        <div className="relative min-w-0 flex-1">
          <SearchIcon size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
          <Input
            value={query}
            onChange={(event) => {
              const nextQuery = event.target.value;
              if (!nextQuery.trim() && !browseMode) resetBrowseNavigation();
              setQuery(nextQuery);
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter') runSearch();
            }}
            placeholder={t('memory.search.placeholder')}
            className="pl-9 text-[13px]"
          />
        </div>
        <Select
          value={project}
          onChange={(event) => {
            const nextProject = event.target.value;
            if (browseMode) resetBrowseNavigation(nextProject);
            setProject(nextProject);
          }}
          aria-label={t('memory.search.projectLabel')}
          wrapperClassName="lg:w-52"
        >
          {projects.map((row) => (
            <option key={row.id} value={row.id}>
              {row.kind === 'default'
                ? t('memory.search.projectDefault')
                : row.kind === 'all'
                  ? t('memory.search.projectAll')
                  : row.id}
            </option>
          ))}
        </Select>
        {browseMode ? (
          <>
            <SegmentedRadio<MemoryOrigin>
              value={browseOrigin}
              onChange={(nextOrigin) => {
                if (nextOrigin === browseOrigin) return;
                resetBrowseNavigation(project, nextOrigin);
                setBrowseOrigin(nextOrigin);
              }}
              options={[
                { id: 'user', label: t('memory.origin.user') },
                { id: 'agent', label: t('memory.origin.agent') },
              ]}
              ariaLabel={t('memory.search.browse.originLabel')}
              className="lg:w-64"
            />
            <Select
              value="newest"
              onChange={() => undefined}
              aria-label={t('memory.search.browse.sortLabel')}
              wrapperClassName="lg:w-44"
            >
              <option value="newest">{t('memory.search.browse.newestFirst')}</option>
            </Select>
          </>
        ) : (
          <Button onClick={runSearch} disabled={searching || !query.trim()}>
            {searching ? <Loader2 className="size-3.5 animate-spin" /> : <SearchIcon className="size-3.5" />}
            {searching ? t('memory.search.searching') : t('memory.search.button')}
          </Button>
        )}
      </div>

      {browseMode ? (
        <>
          {browseData?.warnings.includes('memory_list_partial') ? (
            <div className="rounded-lg border border-border bg-surface px-4 py-3 text-sm text-muted">
              {t('memory.search.browse.partial')}
            </div>
          ) : null}
          {browseData?.warnings.includes('memory_list_truncated') ? (
            <div className="rounded-lg border border-border bg-surface px-4 py-3 text-sm text-muted">
              {t('memory.search.browse.truncated')}
            </div>
          ) : null}
          {browseError ? (
            <div className="flex flex-col gap-3">
              <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive-ink">
                {browseError}
              </div>
              <div className="flex flex-wrap items-center justify-center gap-2">
                <Button variant="secondary" size="sm" onClick={retryBrowsePage}>
                  <RotateCw className="size-3.5" />
                  {t('memory.search.browse.retry')}
                </Button>
                {browsePagination}
              </div>
            </div>
          ) : browsing || !browsed ? (
            <div className="flex min-h-32 items-center justify-center gap-2 rounded-lg border border-border bg-surface text-sm text-muted">
              <Loader2 className="size-4 animate-spin text-mint-ink" />
              {t('memory.search.browse.loading')}
            </div>
          ) : browseItems.length === 0 ? (
            <div className="flex flex-col gap-3">
              <div className="rounded-lg border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
                {project === 'all' && browseIncomplete
                  ? t('memory.search.browse.partialEmpty')
                  : t('memory.search.browse.empty')}
              </div>
              {browsePage > 1 || hasNextBrowsePage ? browsePagination : null}
            </div>
          ) : (
            <section className="flex flex-col gap-3" aria-labelledby="memory-browse-title">
              <div className="flex flex-wrap items-end justify-between gap-2">
                <h3 id="memory-browse-title" className="text-[14px] font-semibold text-foreground">
                  {t('memory.search.browse.episodes')}
                </h3>
                <div className="flex items-center gap-2 font-mono text-[10.5px] uppercase text-muted">
                  {browseData?.total_count != null ? (
                    <span>{t('memory.search.browse.total', { count: browseData.total_count })}</span>
                  ) : null}
                  <span>{t('memory.search.browse.pageSummary', { page: browsePage, total: totalPages })}</span>
                </div>
              </div>
              <div className="overflow-hidden rounded-lg border border-border bg-surface">
                {browseItems.map((item) => {
                  const itemKey = episodeIdentity(item);
                  const selected = itemKey === selectedEpisodeKey;
                  return (
                    <button
                      key={itemKey}
                      type="button"
                      onClick={() => {
                        setSelectedEpisodeKey(itemKey);
                        setCopiedEpisodeKey(null);
                      }}
                      aria-pressed={selected}
                      aria-label={t('memory.search.browse.openDetail', { title: episodeTitle(item) })}
                      className={`block w-full border-b border-border px-4 py-3 text-left last:border-b-0 hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring ${selected ? 'bg-mint/[0.06]' : ''}`}
                    >
                      <div className="mb-1.5 flex items-center justify-between gap-3">
                        <div className="flex min-w-0 items-center gap-1.5">
                          <Badge variant={selected ? 'success' : 'outline'} className="max-w-40 truncate font-mono text-[9.5px] uppercase">
                            {item.project === 'default' ? t('memory.search.projectDefault') : item.project}
                          </Badge>
                          {memoryOriginLabelKey(item.origin) ? (
                            <Badge variant="outline" className="shrink-0">
                              {t(memoryOriginLabelKey(item.origin)!)}
                            </Badge>
                          ) : null}
                        </div>
                        <time dateTime={item.timestamp} className="shrink-0 font-mono text-[10.5px] text-muted">
                          {formatEpisodeTimestamp(item.timestamp)}
                        </time>
                      </div>
                      <p className="line-clamp-2 text-[13px] leading-relaxed text-foreground">
                        {episodeTitle(item)}
                      </p>
                    </button>
                  );
                })}
              </div>

              {browsePagination}

              {selectedEpisode ? (
                <Card aria-labelledby="memory-episode-detail-title">
                  <CardContent className="flex flex-col gap-3 p-4">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <p id="memory-episode-detail-title" className="text-[13px] font-semibold text-foreground">
                          {t('memory.search.browse.detailTitle')}
                        </p>
                        <p className="mt-1 text-[11px] text-muted">{episodeTitle(selectedEpisode)}</p>
                      </div>
                      <time dateTime={selectedEpisode.timestamp} className="font-mono text-[10.5px] text-muted">
                        {formatEpisodeTimestamp(selectedEpisode.timestamp)}
                      </time>
                    </div>
                    <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">
                      {selectedEpisode.body}
                    </p>
                    <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
                      <span className="font-mono text-[10px] uppercase text-muted">
                        {t('memory.search.browse.entryId')}
                      </span>
                      <Button
                        variant="secondary"
                        size="xs"
                        className="max-w-full font-mono"
                        onClick={() => {
                          void copyTextToClipboard(selectedEpisode.id).then((copied) => {
                            if (copied) setCopiedEpisodeKey(episodeIdentity(selectedEpisode));
                          });
                        }}
                        aria-label={copiedEpisodeKey === episodeIdentity(selectedEpisode)
                          ? t('memory.search.browse.entryIdCopied')
                          : t('memory.search.browse.copyEntryId')}
                      >
                        <span className="max-w-[min(60vw,28rem)] truncate">{selectedEpisode.id}</span>
                        {copiedEpisodeKey === episodeIdentity(selectedEpisode)
                          ? <Check className="size-3.5 text-mint-ink" />
                          : <Copy className="size-3.5" />}
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              ) : null}
            </section>
          )}
        </>
      ) : (
        <>
          {searchWarnings.includes('memory_search_partial') ? (
            <div className="rounded-lg border border-border bg-surface px-4 py-3 text-sm text-muted">{t('memory.search.partial')}</div>
          ) : null}
          {searchWarnings.includes('memory_search_truncated') ? (
            <div className="rounded-lg border border-border bg-surface px-4 py-3 text-sm text-muted">{t('memory.search.truncated')}</div>
          ) : null}
          {searchError ? (
            <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive-ink">{searchError}</div>
          ) : !searched ? null : !searchItems || searchItems.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border bg-surface p-8 text-center text-sm text-muted">
              {t('memory.search.empty')}
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {searchItems.map((item, index) => (
                <div key={index} className="rounded-lg border border-border bg-surface px-4 py-3">
                  <div className="mb-1 flex items-center gap-2">
                    <Badge variant="secondary">{t(`memory.kind.${item.kind}`)}</Badge>
                    {memoryOriginLabelKey(item.origin) ? (
                      <Badge variant="outline">{t(memoryOriginLabelKey(item.origin)!)}</Badge>
                    ) : null}
                    {item.project && (project === 'all' || item.project !== 'default') ? (
                      <Badge variant="outline">
                        {item.project === 'default' ? t('memory.search.projectDefault') : item.project}
                      </Badge>
                    ) : null}
                    {item.date ? <span className="font-mono text-[10.5px] text-muted">{item.date}</span> : null}
                  </div>
                  <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-foreground">{item.text}</p>
                </div>
              ))}
              <p className="px-1 text-[11px] text-muted">{t('memory.search.sourceNote')}</p>
            </div>
          )}
        </>
      )}
    </div>
  );
};
