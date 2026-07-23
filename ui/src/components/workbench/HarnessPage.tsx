import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  Activity,
  Calendar,
  Eye,
  Webhook,
  History,
  Plus,
  Play,
  Pause,
  RefreshCw,
  AlertTriangle,
  CheckCircle2,
  Trash2,
  XCircle,
  Loader2,
  Clock,
  PauseCircle,
  Search,
  Bot,
  MessageSquare,
  ArrowUpRight,
  Filter,
  X,
} from 'lucide-react';
import clsx from 'clsx';
import { Link, useSearchParams } from 'react-router-dom';

import { useApi } from '../../context/ApiContext';
import type {
  HarnessDefinitionCounts,
  HarnessDefinitionStatus,
  HarnessRun,
  HarnessRunCounts,
  HarnessRunStatus,
  HarnessSessionSummary,
  HarnessTask,
  HarnessWatch,
  VibeAgentBrief,
} from '../../context/ApiContext';
import { formatRelativeTime } from '../../lib/relativeTime';
import { formatLocalDateTime } from '../../lib/datetime';
import { PlatformIcon } from '../visual/PlatformIcon';
import { CreateViaChatDialog } from './CreateViaChatDialog';
import type { CreateViaChatKind } from './CreateViaChatDialog';
import { CapabilityTabs } from './CapabilityTabs';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Switch } from '../ui/switch';

// Task/watch rows fall back to their id when no name is set, and those
// ids are 32-char hex strings that wreck the row layout. Show the first
// 10 chars + ellipsis so the row still hints at "id-shaped" without
// dominating.
function displayTitle(value: string | null | undefined, fallbackId: string): string {
  if (value && value.trim()) return value;
  if (fallbackId.length <= 13) return fallbackId;
  return `${fallbackId.slice(0, 10)}…`;
}

function formatSchedule(task: HarnessTask, t: (k: string, opts?: any) => string): string {
  if (task.cron) return t('harness.schedule.cron', { value: task.cron });
  if (task.run_at) return t('harness.schedule.oneShot', { value: task.run_at });
  return task.schedule_type || t('harness.unknownSchedule');
}

type TabKey = 'tasks' | 'watches' | 'webhooks' | 'runs';

const TAB_ORDER: TabKey[] = ['tasks', 'watches', 'webhooks', 'runs'];
const PAGE_LIMIT = 30;
const EMPTY_DEFINITION_COUNTS: HarnessDefinitionCounts = { all: 0, enabled: 0, disabled: 0 };
const EMPTY_RUN_COUNTS: HarnessRunCounts = {
  all: 0,
  queued: 0,
  running: 0,
  succeeded: 0,
  failed: 0,
  canceled: 0,
};

type Selection =
  | { kind: 'task'; id: string }
  | { kind: 'watch'; id: string }
  | { kind: 'run'; id: string }
  | null;

export const HarnessPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const [tab, setTab] = useState<TabKey>('tasks');
  const [tasks, setTasks] = useState<HarnessTask[]>([]);
  const [watches, setWatches] = useState<HarnessWatch[]>([]);
  const [runs, setRuns] = useState<HarnessRun[]>([]);
  const [taskCounts, setTaskCounts] = useState<HarnessDefinitionCounts>(EMPTY_DEFINITION_COUNTS);
  const [watchCounts, setWatchCounts] = useState<HarnessDefinitionCounts>(EMPTY_DEFINITION_COUNTS);
  const [runCounts, setRunCounts] = useState<HarnessRunCounts>(EMPTY_RUN_COUNTS);
  const [queryTaskCounts, setQueryTaskCounts] = useState<HarnessDefinitionCounts>(EMPTY_DEFINITION_COUNTS);
  const [queryWatchCounts, setQueryWatchCounts] = useState<HarnessDefinitionCounts>(EMPTY_DEFINITION_COUNTS);
  const [tasksHasMore, setTasksHasMore] = useState(false);
  const [watchesHasMore, setWatchesHasMore] = useState(false);
  const [runsHasMore, setRunsHasMore] = useState(false);
  const [tasksPage, setTasksPage] = useState(1);
  const [watchesPage, setWatchesPage] = useState(1);
  const [runsPage, setRunsPage] = useState(1);
  const [selection, setSelection] = useState<Selection>(null);
  const [selectedRun, setSelectedRun] = useState<HarnessRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Per-id pending state so the row's toggle / delete buttons can show a
  // spinner without disabling siblings.
  const [pendingMutation, setPendingMutation] = useState<Record<string, boolean>>({});
  const [createKind, setCreateKind] = useState<CreateViaChatKind | null>(null);
  // Search + status filter live on the page so the same controls work
  // for tasks and watches; reset between tab switches happens via
  // setSelection(null) below.
  const [search, setSearch] = useState('');
  // Default to enabled-only: the Harness landing view should surface the
  // active tasks/watches first, not bury them among disabled leftovers. The
  // user can still switch to "all"/"disabled"; the filtered-count hint makes
  // the active filter obvious.
  const [statusFilter, setStatusFilter] = useState<HarnessDefinitionStatus>('enabled');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const refreshSeq = useRef(0);
  // URL scope from the background-work banner (spec req 4): ?tab / ?session /
  // ?run deep-link into a session-scoped tab (removable "只看本会话" chip) or a
  // specific run. One-way URL -> state, keyed per-param so a user's tab click
  // (which doesn't touch the URL) is never clobbered by a re-sync.
  const [searchParams, setSearchParams] = useSearchParams();
  const [sessionFilter, setSessionFilter] = useState<string | undefined>(undefined);
  // Global background-work banner toggle (spec req 2), persisted server-side.
  const [bannerEnabled, setBannerEnabled] = useState(true);
  const [bannerPending, setBannerPending] = useState(false);

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedSearch(search.trim()), 250);
    return () => window.clearTimeout(timeout);
  }, [search]);

  const tabParam = searchParams.get('tab');
  const sessionParam = searchParams.get('session');
  const runParam = searchParams.get('run');
  useEffect(() => {
    if (tabParam && (['tasks', 'watches', 'runs'] as string[]).includes(tabParam)) {
      setTab(tabParam as TabKey);
    }
  }, [tabParam]);
  useEffect(() => {
    setSessionFilter(sessionParam || undefined);
    setTasksPage(1);
    setWatchesPage(1);
  }, [sessionParam]);
  useEffect(() => {
    if (runParam) {
      setSelection({ kind: 'run', id: runParam });
    } else {
      // A deep-link that drops ?run (e.g. browser back/forward from a run link
      // to a watch/task session link) must not leave the previous run's detail
      // panel open on the new tab. Only clear a stale RUN anchor — a task/watch
      // row the user clicked stays selected.
      setSelection((prev) => (prev?.kind === 'run' ? null : prev));
    }
  }, [runParam]);

  // Global banner toggle: read once, default ON on any error.
  useEffect(() => {
    let cancelled = false;
    api
      .getWorkbenchPrefs()
      .then((prefs) => {
        if (!cancelled) setBannerEnabled(prefs?.background_work_banner_enabled !== false);
      })
      .catch(() => {
        /* keep default ON */
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

  const onToggleBanner = useCallback(
    async (next: boolean) => {
      setBannerEnabled(next); // optimistic
      setBannerPending(true);
      try {
        const prefs = await api.setBackgroundWorkBannerEnabled(next);
        setBannerEnabled(prefs?.background_work_banner_enabled !== false);
      } catch {
        setBannerEnabled(!next); // revert on failure
      } finally {
        setBannerPending(false);
      }
    },
    [api],
  );

  const clearSessionFilter = useCallback(() => {
    setSessionFilter(undefined);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('session');
        return next;
      },
      { replace: true },
    );
  }, [setSearchParams]);

  const refresh = useCallback(async () => {
    const seq = refreshSeq.current + 1;
    refreshSeq.current = seq;
    const isCurrent = () => refreshSeq.current === seq;
    setLoading(true);
    setError(null);
    const query = tab === 'tasks' || tab === 'watches' ? debouncedSearch || undefined : undefined;
    try {
      if (tab === 'webhooks') {
        const counts = await api.getHarnessCounts();
        if (!isCurrent()) return;
        setTaskCounts(counts.tasks);
        setWatchCounts(counts.watches);
        setRunCounts(counts.runs);
        return;
      }
      const result = await api.getHarnessBootstrap({
        tab,
        status: tab === 'runs' ? undefined : statusFilter,
        query,
        // Session scope applies to definition tabs; runs anchor by ?run instead.
        session_id: tab === 'tasks' || tab === 'watches' ? sessionFilter : undefined,
        page: tab === 'tasks' ? tasksPage : tab === 'watches' ? watchesPage : runsPage,
        limit: PAGE_LIMIT,
      });
      if (!isCurrent()) return;
      setTaskCounts(result.counts.tasks);
      setWatchCounts(result.counts.watches);
      setRunCounts(result.counts.runs);
      if (tab === 'tasks') {
        const page = result.page as Awaited<ReturnType<typeof api.listHarnessTasks>>;
        setTasks(page.tasks);
        setQueryTaskCounts(page.counts);
        setTasksHasMore(page.has_more);
      } else if (tab === 'watches') {
        const page = result.page as Awaited<ReturnType<typeof api.listHarnessWatches>>;
        setWatches(page.watches);
        setQueryWatchCounts(page.counts);
        setWatchesHasMore(page.has_more);
      } else if (tab === 'runs') {
        const page = result.page as Awaited<ReturnType<typeof api.listHarnessRuns>>;
        setRuns(page.runs);
        setRunsHasMore(page.has_more);
      }
    } catch (err: any) {
      if (!isCurrent()) return;
      setError(err?.message ?? String(err));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [api, tab, debouncedSearch, statusFilter, sessionFilter, tasksPage, watchesPage, runsPage]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      onRunsUpdated: () => {
        void refresh();
      },
    });
  }, [api, refresh]);

  // Resolve agent_name → backend/model/effort for the detail panels: the
  // task/watch payload stores only the name. Fetched once on mount.
  const [agentsByName, setAgentsByName] = useState<Record<string, VibeAgentBrief>>({});
  useEffect(() => {
    let cancelled = false;
    api
      .listVibeAgents({ includeDisabled: true })
      .then((res) => {
        if (cancelled) return;
        const map: Record<string, VibeAgentBrief> = {};
        for (const a of res.agents) map[a.name] = a;
        setAgentsByName(map);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api]);

  const markPending = useCallback((id: string, value: boolean) => {
    setPendingMutation((prev) => {
      const next = { ...prev };
      if (value) next[id] = true;
      else delete next[id];
      return next;
    });
  }, []);

  const toggleTaskEnabled = useCallback(
    async (task: HarnessTask) => {
      markPending(task.id, true);
      // Optimistic toggle so the pill flips instantly; rollback on error.
      const next = !task.enabled;
      setTasks((prev) => prev.map((t) => (t.id === task.id ? { ...t, enabled: next } : t)));
      try {
        await api.setHarnessTaskEnabled(task.id, next);
        if (statusFilter !== 'all' && ((statusFilter === 'enabled') !== next)) {
          setSelection((prev) => (prev?.kind === 'task' && prev.id === task.id ? null : prev));
        }
        await refresh();
      } catch (err: any) {
        setError(err?.message ?? String(err));
        setTasks((prev) => prev.map((t) => (t.id === task.id ? { ...t, enabled: task.enabled } : t)));
      } finally {
        markPending(task.id, false);
      }
    },
    [api, markPending, refresh, statusFilter],
  );

  const deleteTask = useCallback(
    async (task: HarnessTask) => {
      const confirmed = window.confirm(
        t('harness.row.deleteConfirmTask', { name: task.name || task.id }),
      );
      if (!confirmed) return;
      markPending(task.id, true);
      try {
        await api.deleteHarnessTask(task.id);
        setSelection((prev) => (prev?.kind === 'task' && prev.id === task.id ? null : prev));
        if (tasks.length === 1 && tasksPage > 1) setTasksPage((page) => Math.max(1, page - 1));
        else await refresh();
      } catch (err: any) {
        setError(err?.message ?? String(err));
      } finally {
        markPending(task.id, false);
      }
    },
    [api, markPending, refresh, t, tasks.length, tasksPage],
  );

  const toggleWatchEnabled = useCallback(
    async (watch: HarnessWatch) => {
      markPending(watch.id, true);
      const next = !watch.enabled;
      setWatches((prev) => prev.map((w) => (w.id === watch.id ? { ...w, enabled: next } : w)));
      try {
        await api.setHarnessWatchEnabled(watch.id, next);
        if (statusFilter !== 'all' && ((statusFilter === 'enabled') !== next)) {
          setSelection((prev) => (prev?.kind === 'watch' && prev.id === watch.id ? null : prev));
        }
        await refresh();
      } catch (err: any) {
        setError(err?.message ?? String(err));
        setWatches((prev) => prev.map((w) => (w.id === watch.id ? { ...w, enabled: watch.enabled } : w)));
      } finally {
        markPending(watch.id, false);
      }
    },
    [api, markPending, refresh, statusFilter],
  );

  const deleteWatch = useCallback(
    async (watch: HarnessWatch) => {
      const confirmed = window.confirm(
        t('harness.row.deleteConfirmWatch', { name: watch.name || watch.id }),
      );
      if (!confirmed) return;
      markPending(watch.id, true);
      try {
        await api.deleteHarnessWatch(watch.id);
        setSelection((prev) => (prev?.kind === 'watch' && prev.id === watch.id ? null : prev));
        if (watches.length === 1 && watchesPage > 1) setWatchesPage((page) => Math.max(1, page - 1));
        else await refresh();
      } catch (err: any) {
        setError(err?.message ?? String(err));
      } finally {
        markPending(watch.id, false);
      }
    },
    [api, markPending, refresh, t, watches.length, watchesPage],
  );

  // Fetch run detail (stdout/stderr) whenever a run is selected so the
  // detail panel always shows the full body, not just the list excerpt.
  useEffect(() => {
    if (selection?.kind !== 'run') {
      setSelectedRun(null);
      return;
    }
    let cancelled = false;
    api
      .getHarnessRun(selection.id)
      .then((result) => {
        if (!cancelled && result.ok) setSelectedRun(result.run);
      })
      .catch((err) => {
        if (!cancelled) setError(err?.message ?? String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [api, selection]);

  const counts = useMemo(
    () => ({
      tasks: taskCounts.all,
      watches: watchCounts.all,
      webhooks: 0,
      runs: runCounts.all,
    }),
    [taskCounts.all, watchCounts.all, runCounts.all],
  );

  const selectedTask = useMemo(
    () => (selection?.kind === 'task' ? tasks.find((task) => task.id === selection.id) ?? null : null),
    [selection, tasks],
  );
  const selectedWatch = useMemo(
    () => (selection?.kind === 'watch' ? watches.find((watch) => watch.id === selection.id) ?? null : null),
    [selection, watches],
  );

  const hasSelection = !!(selectedTask || selectedWatch || selectedRun);
  const showSearchBar = tab === 'tasks' || tab === 'watches';
  const queryCounts = tab === 'tasks' ? queryTaskCounts : queryWatchCounts;
  const totalForTab = queryCounts.all;
  const shownForTab = queryCounts[statusFilter] ?? 0;

  return (
    <div className="mx-auto flex w-full max-w-[1180px] flex-col gap-5 py-2">
      <CapabilityTabs />
      {/* Header */}
      <div className="flex items-center gap-4">
        <div className="flex size-12 shrink-0 items-center justify-center rounded-2xl border border-violet/30 bg-violet/[0.08] text-violet shadow-[0_0_24px_-6px_rgba(124,91,255,0.5)]">
          <Activity className="size-5" />
        </div>
        <div className="flex flex-1 flex-col">
          <h1 className="text-2xl font-bold text-foreground">{t('harness.title')}</h1>
          <p className="text-[13px] text-muted">{t('harness.subtitle')}</p>
        </div>
        {(tab === 'tasks' || tab === 'watches') && (
          <Button
            type="button"
            variant="brand-violet"
            size="xs"
            onClick={() => setCreateKind(tab === 'tasks' ? 'task' : 'watch')}
          >
            <Plus />
            {t('harness.create')}
          </Button>
        )}
        <Button type="button" variant="outline" size="xs" onClick={() => refresh()} disabled={loading}>
          <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} />
          {t('common.refresh')}
        </Button>
      </div>

      {/* Global background-work banner toggle (spec req 2). Off → the workbench
          chat banner never renders in any session; data/API unaffected. */}
      <div className="flex items-center justify-between gap-4 rounded-xl border border-border-strong bg-surface px-4 py-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold text-foreground">{t('harness.bannerToggle.title')}</div>
          <div className="text-[12px] text-muted">{t('harness.bannerToggle.description')}</div>
        </div>
        <Switch
          checked={bannerEnabled}
          onCheckedChange={onToggleBanner}
          disabled={bannerPending}
          label={t('harness.bannerToggle.title')}
        />
      </div>

      {/* Tab row */}
      <div className="flex items-center gap-0 overflow-x-auto border-b border-border">
        {TAB_ORDER.map((key) => {
          const active = tab === key;
          const count = counts[key];
          return (
            <button
              key={key}
              type="button"
              onClick={() => {
                setTab(key);
                setSelection(null);
              }}
              className={clsx(
                'flex shrink-0 items-center gap-2 whitespace-nowrap px-4 py-3 text-[13px] transition',
                active ? 'border-b-2 border-violet font-bold text-violet' : 'font-medium text-muted hover:text-foreground',
              )}
            >
              <HarnessTabIcon tab={key} active={active} />
              {t(`harness.tabs.${key}`)}
              {key !== 'webhooks' && (
                <span
                  className={clsx(
                    'rounded-full border px-1.5 py-0 font-mono text-[9px] font-bold',
                    active
                      ? 'border-violet/30 bg-violet/[0.10] text-violet'
                      : 'border-border-strong bg-foreground/[0.04] text-muted',
                  )}
                >
                  {count}
                </span>
              )}
              {key === 'webhooks' && (
                <span className="font-mono text-[9px] text-muted">{t('harness.soon')}</span>
              )}
            </button>
          );
        })}
      </div>

      {/* Search + status filter — only meaningful for tasks/watches. The
          runs tab has its own server-side query in /api/harness/runs. */}
      {showSearchBar && (
        <div className="flex flex-wrap items-center gap-2.5">
          <div className="flex h-9 w-full items-center gap-2 rounded-md border border-input bg-background px-3 transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring sm:w-[320px]">
            <Search className="size-3.5 shrink-0 text-muted" />
            <input
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setTasksPage(1);
                setWatchesPage(1);
                setSelection(null);
              }}
              placeholder={t('harness.searchPlaceholder')}
              className="flex-1 bg-transparent text-[12px] text-foreground outline-none placeholder:text-muted"
            />
          </div>
          <div className="flex rounded-md border border-border-strong bg-surface p-0.5">
            {(['all', 'enabled', 'disabled'] as const).map((opt) => (
              <button
                key={opt}
                type="button"
                onClick={() => {
                  setStatusFilter(opt);
                  setTasksPage(1);
                  setWatchesPage(1);
                  setSelection(null);
                }}
                className={clsx(
                  'rounded px-2.5 py-1 text-[11px] font-medium transition',
                  statusFilter === opt
                    ? 'bg-violet/[0.12] text-violet'
                    : 'text-muted hover:text-foreground',
                )}
              >
                {t(`harness.statusFilter.${opt}`)}
              </button>
            ))}
          </div>
          {sessionFilter && (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-cyan/30 bg-cyan/[0.10] py-1 pl-2.5 pr-1.5 text-[11px] font-medium text-cyan">
              <Filter className="size-3 shrink-0" />
              <span className="max-w-[180px] truncate">
                {t('harness.sessionFilter.chip', { id: sessionFilter })}
              </span>
              <button
                type="button"
                onClick={clearSessionFilter}
                aria-label={t('harness.sessionFilter.clear')}
                title={t('harness.sessionFilter.clear')}
                className="rounded-full p-0.5 text-cyan/80 transition-colors hover:bg-cyan/20 hover:text-cyan"
              >
                <X className="size-3" />
              </button>
            </span>
          )}
          {(search || statusFilter !== 'all') && (
            <span className="ml-auto font-mono text-[10px] text-muted">
              {t('harness.filtered', { shown: shownForTab, total: totalForTab })}
            </span>
          )}
        </div>
      )}

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive">
          {error}
        </div>
      )}

      {/* Body — list takes the leftover space; detail card only renders
          when something is selected. ``minmax(0,1fr)`` keeps the list
          column from refusing to shrink, which was letting long rows
          push the right-side card past the viewport edge. */}
      <div
        className={clsx(
          'grid gap-5',
          hasSelection ? 'grid-cols-1 lg:grid-cols-[minmax(0,1fr)_440px]' : 'grid-cols-1',
        )}
      >
        <div className={clsx('flex min-w-0 flex-col gap-2', hasSelection && 'max-lg:hidden')}>
          {tab === 'tasks' && (
            <TasksList
              tasks={tasks}
              loading={loading}
              selectedId={selection?.kind === 'task' ? selection.id : null}
              onSelect={(id) => setSelection({ kind: 'task', id })}
              onToggleEnabled={toggleTaskEnabled}
              onDelete={deleteTask}
              pending={pendingMutation}
              page={tasksPage}
              hasMore={tasksHasMore}
              onPageChange={(page) => {
                setTasksPage(page);
                setSelection(null);
              }}
            />
          )}
          {tab === 'watches' && (
            <WatchesList
              watches={watches}
              loading={loading}
              selectedId={selection?.kind === 'watch' ? selection.id : null}
              onSelect={(id) => setSelection({ kind: 'watch', id })}
              onToggleEnabled={toggleWatchEnabled}
              onDelete={deleteWatch}
              pending={pendingMutation}
              page={watchesPage}
              hasMore={watchesHasMore}
              onPageChange={(page) => {
                setWatchesPage(page);
                setSelection(null);
              }}
            />
          )}
          {tab === 'webhooks' && <WebhooksEmpty />}
          {tab === 'runs' && (
            <RunsList
              runs={runs}
              loading={loading}
              selectedId={selection?.kind === 'run' ? selection.id : null}
              onSelect={(id) => setSelection({ kind: 'run', id })}
              page={runsPage}
              hasMore={runsHasMore}
              onPageChange={(page) => {
                setRunsPage(page);
                setSelection(null);
              }}
            />
          )}
        </div>

        {hasSelection && (
          <div className="flex min-w-0 flex-col gap-3 self-start rounded-xl border border-border-strong bg-surface p-5">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setSelection(null)}
              className="-mt-1 h-auto gap-1.5 self-start px-0 text-[12px] font-medium text-muted hover:bg-transparent hover:text-foreground lg:hidden"
            >
              <ArrowLeft className="size-3.5" />
              {t('common.back')}
            </Button>
            {selectedTask ? (
              <TaskDetail
                task={selectedTask}
                agent={agentsByName[selectedTask.agent_name ?? '']}
                onToggleEnabled={() => toggleTaskEnabled(selectedTask)}
                pending={!!pendingMutation[selectedTask.id]}
              />
            ) : selectedWatch ? (
              <WatchDetail
                watch={selectedWatch}
                agent={agentsByName[selectedWatch.agent_name ?? '']}
                onToggleEnabled={() => toggleWatchEnabled(selectedWatch)}
                pending={!!pendingMutation[selectedWatch.id]}
              />
            ) : selectedRun ? (
              <RunDetail run={selectedRun} />
            ) : null}
          </div>
        )}
      </div>

      {createKind && (
        <CreateViaChatDialog kind={createKind} onClose={() => setCreateKind(null)} />
      )}
    </div>
  );
};

interface TabIconProps {
  tab: TabKey;
  active: boolean;
}

const HarnessTabIcon: React.FC<TabIconProps> = ({ tab, active }) => {
  const cls = clsx('size-3.5', active ? 'text-violet' : 'text-muted');
  if (tab === 'tasks') return <Calendar className={cls} />;
  if (tab === 'watches') return <Eye className={cls} />;
  if (tab === 'webhooks') return <Webhook className={cls} />;
  return <History className={cls} />;
};

interface HarnessPagerProps {
  page: number;
  hasMore: boolean;
  onPageChange: (page: number) => void;
}

const HarnessPager: React.FC<HarnessPagerProps> = ({ page, hasMore, onPageChange }) => {
  const { t } = useTranslation();
  if (page <= 1 && !hasMore) return null;
  return (
    <div className="mt-2 flex items-center justify-end gap-2 px-1">
      <Button
        type="button"
        variant="outline"
        size="xs"
        disabled={page <= 1}
        onClick={() => onPageChange(page - 1)}
        className="h-7 px-2 font-mono text-[10px]"
      >
        {t('common.previous')}
      </Button>
      <span className="font-mono text-[10px] text-muted">{t('harness.pageLabel', { page })}</span>
      <Button
        type="button"
        variant="outline"
        size="xs"
        disabled={!hasMore}
        onClick={() => onPageChange(page + 1)}
        className="h-7 px-2 font-mono text-[10px]"
      >
        {t('common.next')}
      </Button>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Tasks tab
// ---------------------------------------------------------------------------

interface TasksListProps {
  tasks: HarnessTask[];
  loading: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onToggleEnabled: (task: HarnessTask) => void;
  onDelete: (task: HarnessTask) => void;
  pending: Record<string, boolean>;
  page: number;
  hasMore: boolean;
  onPageChange: (page: number) => void;
}

const TasksList: React.FC<TasksListProps> = ({
  tasks,
  loading,
  selectedId,
  onSelect,
  onToggleEnabled,
  onDelete,
  pending,
  page,
  hasMore,
  onPageChange,
}) => {
  const { t } = useTranslation();
  if (tasks.length === 0 && !loading) return <EmptyState i18nKey="harness.emptyTasks" />;
  return (
    <>
      {tasks.map((task) => {
        const active = selectedId === task.id;
        const isPending = !!pending[task.id];
        const title = displayTitle(task.name, task.id);
        return (
          <div
            key={task.id}
            className={clsx(
              'group/row flex min-w-0 items-center gap-3 rounded-lg border px-4 py-3 transition',
              active ? 'border-violet/40 bg-violet/[0.05]' : 'border-border bg-surface hover:bg-foreground/[0.03]',
            )}
          >
            <button
              type="button"
              onClick={() => onSelect(task.id)}
              className="flex min-w-0 flex-1 items-center gap-3 text-left"
            >
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-[14px] font-semibold text-foreground" title={task.name || task.id}>
                    {title}
                  </span>
                  <Badge
                    variant={task.enabled ? 'success' : 'secondary'}
                    className="font-mono text-[9px] uppercase"
                  >
                    {task.enabled ? t('harness.runtime.enabled') : t('harness.runtime.disabled')}
                  </Badge>
                </div>
                <div className="flex items-center gap-3 truncate text-[11px] text-muted">
                  <span className="inline-flex items-center gap-1 truncate font-mono">
                    <Clock className="size-3 shrink-0" />
                    {formatSchedule(task, t)}
                  </span>
                  {task.agent_name && <span className="shrink-0">· {task.agent_name}</span>}
                </div>
              </div>
              {task.last_run_at && (
                <span className="shrink-0 font-mono text-[10px] text-muted">
                  {formatRelativeTime(task.last_run_at, t)}
                </span>
              )}
            </button>
            <RowActions
              enabled={task.enabled}
              pending={isPending}
              onToggle={() => onToggleEnabled(task)}
              onDelete={() => onDelete(task)}
            />
          </div>
        );
      })}
      <HarnessPager page={page} hasMore={hasMore} onPageChange={onPageChange} />
    </>
  );
};

interface RowActionsProps {
  enabled: boolean;
  pending: boolean;
  onToggle: () => void;
  onDelete: () => void;
}

// Desktop-only hover action cluster. Mobile opens the detail panel first, where
// destructive/enable controls are explicit instead of invisible row hit targets.
const RowActions: React.FC<RowActionsProps> = ({ enabled, pending, onToggle, onDelete }) => {
  const { t } = useTranslation();
  return (
    <div className="pointer-events-none hidden items-center gap-1 opacity-0 transition-opacity group-hover/row:pointer-events-auto group-hover/row:opacity-100 md:flex">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onToggle();
        }}
        disabled={pending}
        aria-label={enabled ? t('harness.row.disable') : t('harness.row.enable')}
        title={enabled ? t('harness.row.disable') : t('harness.row.enable')}
        className={clsx(
          'flex size-7 items-center justify-center rounded-md border transition',
          enabled
            ? 'border-border-strong text-muted hover:bg-foreground/[0.06] hover:text-foreground'
            : 'border-mint/40 bg-mint/[0.08] text-mint hover:brightness-110',
          pending && 'cursor-wait opacity-60',
        )}
      >
        {pending ? (
          <Loader2 className="size-3 animate-spin" />
        ) : enabled ? (
          <Pause className="size-3" />
        ) : (
          <Play className="size-3" />
        )}
      </button>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onDelete();
        }}
        disabled={pending}
        aria-label={t('harness.row.delete')}
        title={t('harness.row.delete')}
        className={clsx(
          'flex size-7 items-center justify-center rounded-md border border-border-strong text-muted transition',
          'hover:border-pink/40 hover:bg-pink/[0.08] hover:text-pink',
          pending && 'cursor-wait opacity-60',
        )}
      >
        <Trash2 className="size-3" />
      </button>
    </div>
  );
};

interface TaskDetailProps {
  task: HarnessTask;
  agent?: VibeAgentBrief;
  onToggleEnabled: () => void;
  pending: boolean;
}

const TaskDetail: React.FC<TaskDetailProps> = ({ task, agent, onToggleEnabled, pending }) => {
  const { t } = useTranslation();
  const title = displayTitle(task.name, task.id);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex min-w-0 items-center gap-2">
        <Calendar className="size-4 shrink-0 text-violet" />
        <div className="min-w-0 flex-1 truncate text-[15px] font-bold text-foreground" title={task.name || task.id}>
          {title}
        </div>
        <StatusPill enabled={task.enabled} />
        <Switch
          checked={task.enabled}
          onCheckedChange={onToggleEnabled}
          label={t(task.enabled ? 'harness.row.disable' : 'harness.row.enable')}
          disabled={pending}
        />
      </div>
      <DetailField label={t('harness.detail.schedule')}>
        <span className="font-mono text-[12px] text-foreground">
          {task.cron ?? task.run_at ?? task.schedule_type ?? '—'}
        </span>
        {task.timezone && <span className="ml-2 text-[10px] text-muted">{task.timezone}</span>}
      </DetailField>
      {task.next_run_at && (
        <DetailField label={t('harness.detail.nextRun')}>
          <span className="font-mono text-[12px] text-foreground">{formatLocalDateTime(task.next_run_at)}</span>
        </DetailField>
      )}
      <DetailField label={t('harness.detail.agent')}>
        <DetailAgent agentName={task.agent_name} agent={agent} />
      </DetailField>
      <DetailField label={t('harness.detail.session')}>
        <DetailSession summary={task} sessionId={task.session_id} />
      </DetailField>
      <div className="grid grid-cols-2 gap-4">
        <DetailField label={t('harness.detail.sessionPolicy')}>
          <span className="text-[12px] text-foreground">{sessionPolicyLabel(task.session_policy, t)}</span>
        </DetailField>
        <DetailField label={t('harness.detail.delivery')}>
          <span className="text-[12px] text-foreground">{deliveryLabel(task.post_to, t)}</span>
        </DetailField>
      </div>
      <DetailField label={t('harness.detail.message')}>
        <pre className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[11px] text-foreground">
          {task.message || task.prompt || '—'}
        </pre>
      </DetailField>
      {task.last_run_at && (
        <DetailField label={t('harness.detail.lastRun')}>
          <span className="font-mono text-[11px] text-muted">{formatLocalDateTime(task.last_run_at)}</span>
          {task.last_error && (
            <div className="mt-1 rounded-md border border-destructive/40 bg-destructive/[0.06] px-2 py-1 text-[11px] text-destructive">
              {task.last_error}
            </div>
          )}
        </DetailField>
      )}
      <DetailField label={t('harness.detail.id')}>
        <code className="font-mono text-[11px] text-muted">{task.id}</code>
      </DetailField>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Watches tab
// ---------------------------------------------------------------------------

interface WatchesListProps {
  watches: HarnessWatch[];
  loading: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onToggleEnabled: (watch: HarnessWatch) => void;
  onDelete: (watch: HarnessWatch) => void;
  pending: Record<string, boolean>;
  page: number;
  hasMore: boolean;
  onPageChange: (page: number) => void;
}

const WatchesList: React.FC<WatchesListProps> = ({
  watches,
  loading,
  selectedId,
  onSelect,
  onToggleEnabled,
  onDelete,
  pending,
  page,
  hasMore,
  onPageChange,
}) => {
  const { t } = useTranslation();
  if (watches.length === 0 && !loading) return <EmptyState i18nKey="harness.emptyWatches" />;
  return (
    <>
      {watches.map((watch) => {
        const active = selectedId === watch.id;
        const isPending = !!pending[watch.id];
        const cmd = watch.shell_command || (Array.isArray(watch.command) ? watch.command.join(' ') : '') || '—';
        const title = displayTitle(watch.name, watch.id);
        return (
          <div
            key={watch.id}
            className={clsx(
              'group/row flex min-w-0 items-center gap-3 rounded-lg border px-4 py-3 transition',
              active ? 'border-violet/40 bg-violet/[0.05]' : 'border-border bg-surface hover:bg-foreground/[0.03]',
            )}
          >
            <button
              type="button"
              onClick={() => onSelect(watch.id)}
              className="flex min-w-0 flex-1 items-center gap-3 text-left"
            >
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-[14px] font-semibold text-foreground" title={watch.name || watch.id}>
                    {title}
                  </span>
                  {watch.runtime.running ? (
                    <Badge variant="success" className="font-mono text-[9px] uppercase">
                      <span className="size-1.5 rounded-full bg-mint" />
                      {t('harness.runtime.running')}
                    </Badge>
                  ) : !watch.enabled ? (
                    <Badge variant="secondary" className="font-mono text-[9px] uppercase">
                      <PauseCircle className="size-2.5" />
                      {t('harness.runtime.paused')}
                    </Badge>
                  ) : (
                    <Badge variant="secondary" className="font-mono text-[9px] uppercase">
                      {t('harness.runtime.idle')}
                    </Badge>
                  )}
                </div>
                <div className="truncate font-mono text-[11px] text-muted">{cmd}</div>
              </div>
              {watch.last_event_at && (
                <span className="shrink-0 font-mono text-[10px] text-muted">
                  {formatRelativeTime(watch.last_event_at, t)}
                </span>
              )}
            </button>
            <RowActions
              enabled={watch.enabled}
              pending={isPending}
              onToggle={() => onToggleEnabled(watch)}
              onDelete={() => onDelete(watch)}
            />
          </div>
        );
      })}
      {watches.length === 0 && loading && <div className="px-4 py-6 text-[12px] text-muted">{t('common.loading')}</div>}
      <HarnessPager page={page} hasMore={hasMore} onPageChange={onPageChange} />
    </>
  );
};

interface WatchDetailProps {
  watch: HarnessWatch;
  agent?: VibeAgentBrief;
  onToggleEnabled: () => void;
  pending: boolean;
}

const WatchDetail: React.FC<WatchDetailProps> = ({ watch, agent, onToggleEnabled, pending }) => {
  const { t } = useTranslation();
  const cmd = watch.shell_command || (Array.isArray(watch.command) ? watch.command.join(' ') : '') || '—';
  const title = displayTitle(watch.name, watch.id);
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex min-w-0 items-center gap-2">
        <Eye className="size-4 shrink-0 text-violet" />
        <div className="min-w-0 flex-1 truncate text-[15px] font-bold text-foreground" title={watch.name || watch.id}>
          {title}
        </div>
        <StatusPill enabled={watch.enabled} runtimeRunning={watch.runtime.running} />
        <Switch
          checked={watch.enabled}
          onCheckedChange={onToggleEnabled}
          label={t(watch.enabled ? 'harness.row.disable' : 'harness.row.enable')}
          disabled={pending}
        />
      </div>
      <DetailField label={t('harness.detail.command')}>
        <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[11px] text-foreground">
          {cmd}
        </pre>
      </DetailField>
      <DetailField label={t('harness.detail.agent')}>
        <DetailAgent agentName={watch.agent_name} agent={agent} />
      </DetailField>
      <DetailField label={t('harness.detail.session')}>
        <DetailSession summary={watch} sessionId={watch.session_id} />
      </DetailField>
      <div className="grid grid-cols-2 gap-4">
        <DetailField label={t('harness.detail.sessionPolicy')}>
          <span className="text-[12px] text-foreground">{sessionPolicyLabel(watch.session_policy, t)}</span>
        </DetailField>
        <DetailField label={t('harness.detail.delivery')}>
          <span className="text-[12px] text-foreground">{deliveryLabel(watch.post_to, t)}</span>
        </DetailField>
      </div>
      <DetailField label={t('harness.detail.cwd')}>
        <code className="font-mono text-[11px] text-muted">{watch.cwd || '—'}</code>
      </DetailField>
      <DetailField label={t('harness.detail.mode')}>
        <span className="font-mono text-[11px] text-muted">{watch.mode}</span>
      </DetailField>
      <DetailField label={t('harness.detail.followUp')}>
        <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[11px] text-foreground">
          {watch.message || watch.prefix || '—'}
        </pre>
      </DetailField>
      {watch.runtime.running && watch.runtime.pid != null && (
        <DetailField label={t('harness.detail.runtime')}>
          <span className="font-mono text-[11px] text-muted">
            pid {watch.runtime.pid} · {formatLocalDateTime(watch.runtime.started_at)}
          </span>
        </DetailField>
      )}
      {watch.last_error && (
        <DetailField label={t('harness.detail.lastError')}>
          <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-2 py-1 text-[11px] text-destructive">
            {watch.last_error}
          </div>
        </DetailField>
      )}
      <DetailField label={t('harness.detail.id')}>
        <code className="font-mono text-[11px] text-muted">{watch.id}</code>
      </DetailField>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Webhooks tab — coming soon
// ---------------------------------------------------------------------------

const WebhooksEmpty: React.FC = () => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-surface px-6 py-16 text-center">
      <Webhook className="size-8 text-muted" />
      <div className="text-[14px] font-semibold text-foreground">{t('harness.webhooksSoon')}</div>
      <div className="max-w-md text-[12px] text-muted">{t('harness.webhooksSoonBody')}</div>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Runs tab
// ---------------------------------------------------------------------------

interface RunsListProps {
  runs: HarnessRun[];
  loading: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  page: number;
  hasMore: boolean;
  onPageChange: (page: number) => void;
}

const RunsList: React.FC<RunsListProps> = ({ runs, loading, selectedId, onSelect, page, hasMore, onPageChange }) => {
  const { t } = useTranslation();
  if (runs.length === 0 && !loading) return <EmptyState i18nKey="harness.emptyRuns" />;
  return (
    <>
      {runs.map((run) => {
        const active = selectedId === run.id;
        return (
          <button
            key={run.id}
            type="button"
            onClick={() => onSelect(run.id)}
            className={clsx(
              'flex items-center gap-3 rounded-lg border px-4 py-3 text-left transition',
              active ? 'border-violet/40 bg-violet/[0.05]' : 'border-border bg-surface hover:bg-foreground/[0.03]',
            )}
          >
            <RunStatusIcon status={run.status} />
            <div className="flex flex-1 flex-col gap-1">
              <div className="flex items-center gap-2">
                <span className="font-mono text-[12px] font-semibold text-foreground">{run.id}</span>
                <span className="rounded border border-border-strong bg-foreground/[0.04] px-1.5 py-0 font-mono text-[9px] text-muted">
                  {run.run_type || 'run'}
                </span>
              </div>
              <div className="flex items-center gap-3 text-[11px] text-muted">
                <span>{run.agent_name || '—'}</span>
                {run.created_at && <span>· {formatRelativeTime(run.created_at, t)}</span>}
              </div>
            </div>
          </button>
        );
      })}
      <HarnessPager page={page} hasMore={hasMore} onPageChange={onPageChange} />
    </>
  );
};

interface RunDetailProps {
  run: HarnessRun;
}

const RunDetail: React.FC<RunDetailProps> = ({ run }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <RunStatusIcon status={run.status} />
        <code className="flex-1 truncate font-mono text-[13px] font-bold text-foreground">{run.id}</code>
        <span
          className={clsx(
            'rounded border px-2 py-0 font-mono text-[9px] font-bold uppercase',
            STATUS_PILL_CLASS[run.status as HarnessRunStatus] ?? 'border-border-strong bg-foreground/[0.04] text-muted',
          )}
        >
          {run.status}
        </span>
      </div>
      <DetailField label={t('harness.detail.type')}>
        <span className="font-mono text-[11px] text-muted">{run.run_type || run.request_type || '—'}</span>
      </DetailField>
      <DetailField label={t('harness.detail.agent')}>
        <span className="text-[12px] text-foreground">{run.agent_name || '—'}</span>
        {run.agent_backend && <span className="ml-2 font-mono text-[10px] text-muted">{run.agent_backend}</span>}
        {run.model && <span className="ml-2 font-mono text-[10px] text-muted">{run.model}</span>}
      </DetailField>
      {run.definition_id && (
        <DetailField label={t('harness.detail.definition')}>
          <code className="font-mono text-[11px] text-muted">{run.definition_id}</code>
        </DetailField>
      )}
      {/* Session + lineage (Part B): the run row already carries these; surface
          them instead of hiding the who-started-whom / where-it-reports story.
          Render the id as plain text — the run payload has no openability flag,
          and a private ``vibe agent run`` session lives on the internal
          private_agent_run pseudo-scope that is intentionally NOT chat-openable,
          so an unconditional /chat link would dead-link. The openable-gated
          chat entry point lives in the Agents 运行 graph detail panel. */}
      {run.session_id && (
        <DetailField label={t('harness.detail.session')}>
          <code className="min-w-0 truncate font-mono text-[11px] text-muted">{run.session_id}</code>
        </DetailField>
      )}
      {(run.source_kind || run.source_actor) && (
        <DetailField label={t('harness.detail.source')}>
          <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5 text-[12px] text-foreground">
            {run.source_kind && (
              <span className="rounded border border-border-strong bg-foreground/[0.04] px-1.5 py-0 font-mono text-[10px] uppercase text-muted">
                {run.source_kind}
              </span>
            )}
            {run.source_actor && (
              // Lineage id only — the run payload carries no openable_in_chat
              // signal, and a stale/imported or internal (non-openable) session
              // id would deep-link to a dead /chat target, so render it plain.
              <span className="font-mono text-[11px] text-muted">{run.source_actor}</span>
            )}
          </span>
        </DetailField>
      )}
      {run.parent_run_id && (
        <DetailField label={t('harness.detail.parentRun')}>
          <Link
            to={`/harness?tab=runs&run=${encodeURIComponent(run.parent_run_id)}`}
            className="inline-flex items-center gap-1 font-mono text-[11px] text-violet hover:underline"
          >
            {run.parent_run_id}
            <ArrowUpRight className="size-3" />
          </Link>
        </DetailField>
      )}
      {run.callback_session_id && (
        <DetailField label={t('harness.detail.callback')}>
          <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5">
            {/* Lineage id only (see source above) — plain text, not a /chat link. */}
            <span className="min-w-0 truncate font-mono text-[11px] text-muted">
              {run.callback_session_id}
            </span>
            {run.callback_status && (
              <span className="rounded border border-border-strong bg-foreground/[0.04] px-1.5 py-0 font-mono text-[10px] uppercase text-muted">
                {run.callback_status}
              </span>
            )}
          </span>
          {run.callback_error && (
            <div className="mt-1 rounded-md border border-destructive/40 bg-destructive/[0.06] px-2 py-1 text-[11px] text-destructive">
              {run.callback_error}
            </div>
          )}
        </DetailField>
      )}
      {(run.message || run.prompt) && (
        <DetailField label={t('harness.detail.message')}>
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[11px] text-foreground">
            {run.message || run.prompt}
          </pre>
        </DetailField>
      )}
      {run.result_text && (
        <DetailField label={t('harness.detail.result')}>
          <pre className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[11px] text-foreground">
            {run.result_text}
          </pre>
        </DetailField>
      )}
      {run.error && (
        <DetailField label={t('harness.detail.error')}>
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md border border-destructive/40 bg-destructive/[0.06] p-2 font-mono text-[11px] text-destructive">
            {run.error}
          </pre>
        </DetailField>
      )}
      {run.stdout && (
        <DetailField label="stdout">
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[10px] text-foreground">
            {run.stdout}
          </pre>
        </DetailField>
      )}
      {run.stderr && (
        <DetailField label="stderr">
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[10px] text-foreground">
            {run.stderr}
          </pre>
        </DetailField>
      )}
      <DetailField label={t('harness.detail.timing')}>
        <div className="flex flex-col gap-0.5 font-mono text-[10px] text-muted">
          <span>created {formatLocalDateTime(run.created_at)}</span>
          {run.started_at && <span>started {formatLocalDateTime(run.started_at)}</span>}
          {run.completed_at && <span>completed {formatLocalDateTime(run.completed_at)}</span>}
          {run.exit_code != null && <span>exit_code {run.exit_code}</span>}
          {run.pid != null && <span>pid {run.pid}</span>}
        </div>
      </DetailField>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

const STATUS_PILL_CLASS: Record<HarnessRunStatus, string> = {
  queued: 'border-cyan/30 bg-cyan/[0.08] text-cyan',
  running: 'border-violet/30 bg-violet/[0.08] text-violet',
  succeeded: 'border-mint/30 bg-mint/[0.08] text-mint',
  failed: 'border-pink/30 bg-pink/[0.08] text-pink',
  canceled: 'border-border-strong bg-foreground/[0.04] text-muted',
};

const RunStatusIcon: React.FC<{ status: HarnessRunStatus }> = ({ status }) => {
  const cls = 'size-4 shrink-0';
  if (status === 'succeeded') return <CheckCircle2 className={clsx(cls, 'text-mint')} />;
  if (status === 'failed') return <XCircle className={clsx(cls, 'text-pink')} />;
  if (status === 'running') return <Loader2 className={clsx(cls, 'animate-spin text-violet')} />;
  if (status === 'queued') return <Clock className={clsx(cls, 'text-cyan')} />;
  if (status === 'canceled') return <AlertTriangle className={clsx(cls, 'text-muted')} />;
  return <Activity className={clsx(cls, 'text-muted')} />;
};

interface StatusPillProps {
  enabled: boolean;
  runtimeRunning?: boolean;
}

const StatusPill: React.FC<StatusPillProps> = ({ enabled, runtimeRunning }) => {
  const { t } = useTranslation();
  if (runtimeRunning) {
    return (
      <Badge variant="success" className="font-mono text-[9px] uppercase">
        <span className="size-1.5 rounded-full bg-mint" />
        {t('harness.runtime.running')}
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="font-mono text-[9px] uppercase">
      {enabled ? t('harness.runtime.enabled') : t('harness.runtime.disabled')}
    </Badge>
  );
};

function sessionPolicyLabel(policy: string | null | undefined, t: (k: string) => string): string {
  if (policy === 'create_per_run') return t('harness.sessionPolicy.createPerRun');
  if (policy === 'create_once') return t('harness.sessionPolicy.createOnce');
  return t('harness.sessionPolicy.existing');
}

function deliveryLabel(postTo: string | null | undefined, t: (k: string) => string): string {
  if (postTo === 'channel') return t('harness.delivery.channel');
  if (postTo === 'thread') return t('harness.delivery.thread');
  return t('harness.delivery.session');
}

// Agent executor: name + resolved backend·model·effort, with a jump to the
// Agents page. agent_name can be null (the definition inherits the scope /
// global default); model/effort can be null (backend default).
const DetailAgent: React.FC<{ agentName: string | null; agent?: VibeAgentBrief }> = ({ agentName, agent }) => {
  const { t } = useTranslation();
  if (!agentName) {
    return <span className="text-[12px] text-muted">{t('harness.detail.agentInherit')}</span>;
  }
  const meta = agent
    ? [
        agent.backend,
        agent.model,
        agent.reasoning_effort ? t('harness.detail.effort', { value: agent.reasoning_effort }) : null,
      ]
        .filter(Boolean)
        .join(' · ')
    : '';
  return (
    <div className="flex min-w-0 items-center gap-2">
      <Bot className="size-3.5 shrink-0 text-violet" />
      <span className="shrink-0 text-[12px] font-medium text-foreground">{agentName}</span>
      {meta && <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted">{meta}</span>}
      <Link
        to="/agents"
        className="ml-auto inline-flex shrink-0 items-center gap-0.5 text-[11px] font-medium text-violet hover:underline"
      >
        {t('harness.detail.openInAgents')}
        <ArrowUpRight className="size-3" />
      </Link>
    </div>
  );
};

// Bound session. Workbench sessions show their title and link to the chat; IM
// sessions show platform + channel and are intentionally not linkable.
const DetailSession: React.FC<{ summary: HarnessSessionSummary; sessionId: string | null }> = ({
  summary,
  sessionId,
}) => {
  const { t } = useTranslation();
  if (!summary.session_is_workbench && !summary.session_platform && !sessionId) {
    return <span className="text-[12px] text-muted">{t('harness.detail.sessionNone')}</span>;
  }
  if (summary.session_is_workbench) {
    const label = summary.session_title || sessionId || '—';
    const body = (
      <>
        <MessageSquare className="size-3.5 shrink-0 text-cyan" />
        <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-foreground">{label}</span>
        {sessionId && <ArrowUpRight className="size-3.5 shrink-0 text-cyan" />}
      </>
    );
    return sessionId ? (
      <Link to={`/chat/${sessionId}`} className="flex min-w-0 items-center gap-2 hover:underline">
        {body}
      </Link>
    ) : (
      <div className="flex min-w-0 items-center gap-2">{body}</div>
    );
  }
  return (
    <div className="flex min-w-0 items-center gap-2">
      {summary.session_platform && <PlatformIcon platform={summary.session_platform} size={14} />}
      <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-foreground">
        {summary.session_label || summary.session_title || sessionId || '—'}
      </span>
      {summary.session_platform && (
        <span className="shrink-0 font-mono text-[10px] uppercase tracking-wide text-muted">
          {summary.session_platform}
        </span>
      )}
    </div>
  );
};

interface DetailFieldProps {
  label: string;
  children: React.ReactNode;
}

const DetailField: React.FC<DetailFieldProps> = ({ label, children }) => (
  <div className="flex flex-col gap-1.5">
    <div className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{label}</div>
    <div>{children}</div>
  </div>
);

const EmptyState: React.FC<{ i18nKey: string }> = ({ i18nKey }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center">
      <Activity className="size-6 text-muted" />
      <div className="text-[13px] text-muted">{t(i18nKey)}</div>
    </div>
  );
};
