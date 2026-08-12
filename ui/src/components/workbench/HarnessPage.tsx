import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ArrowLeft,
  Activity,
  Calendar,
  ChevronRight,
  Eye,
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
  MessageSquareOff,
  ArrowUpRight,
  Filter,
  X,
} from 'lucide-react';
import clsx from 'clsx';
import { Link, useSearchParams } from 'react-router-dom';

import { useApi } from '../../context/ApiContext';
import { DEFAULT_TAB, harnessEmptyStateKey, harnessTabFromParam, TAB_ORDER, type TabKey } from './harnessTabs';
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
import { formatElapsed, runElapsedSeconds } from '../../lib/agentGraph';
import { useVisibleNow } from '../../lib/useVisibleNow';
import { PlatformIcon } from '../visual/PlatformIcon';
import { CreateViaChatDialog } from './CreateViaChatDialog';
import type { CreateViaChatKind } from './CreateViaChatDialog';
import { CapabilityTabs } from './CapabilityTabs';
import {
  BLANK_SESSION_SUMMARY,
  DEFAULT_HIDDEN_RUN_TYPES,
  harnessSessionState,
  runRowTitle,
  runStatusLabel,
  runTypeLabel,
  runTypeOptions,
} from './harnessRuns';
import {
  DEFAULT_DEFINITION_STATUS,
  DEFINITION_STATUS_FILTERS,
  definitionActiveCount,
  definitionChipLabel,
  definitionExitCodeTone,
  definitionHasNeutralWatchExit,
  definitionHealth,
  definitionFailureSummaryKey,
  definitionProcessingHealth,
  definitionRowLine,
  definitionRowTitle,
  definitionStatusCount,
  definitionSurvivesToggle,
  formatCommandLine,
  formatWallTime,
  humanizeCron,
  humanizeTime,
  isWallClockTimestamp,
  lifecycleLabel,
  runCommandSnapshotLine,
  taskCommandPreview,
  taskIsCommand,
  taskOnFailure,
  taskTimeout,
  waiterExpectedAlive,
} from './harnessLifecycle';
import type {
  HarnessDefinitionKind,
  HarnessLifecycleState,
  HarnessRowAlert,
} from './harnessLifecycle';
import { agentDisplayName, loadHarnessAgentCatalog } from './harnessAgents';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Switch } from '../ui/switch';
import { errorMessage } from '@/lib/errorMessage';

// Detail-panel schedule, in words. The literal it was derived from is printed
// beside it by the caller — humanizing must never be the only copy of a value
// an operator may need to paste back into a CLI.
function formatSchedule(task: HarnessTask, t: (k: string, opts?: any) => string): string {
  if (task.cron) return humanizeCron(task.cron, t);
  // A one-shot's ``run_at`` is stored as the user typed it, which is usually a
  // wall-clock reading in ``task.timezone`` with no offset. The scheduler has
  // already resolved it against that zone — take its answer. Only when it
  // promises nothing (already fired, or paused) fall back to the literal, and
  // then say which zone the clock belongs to instead of implying the viewer's.
  if (task.run_at) {
    if (task.next_run_at) return humanizeTime(task.next_run_at, t);
    if (isWallClockTimestamp(task.run_at)) return formatWallTime(task.run_at, task.timezone, t);
    return humanizeTime(task.run_at, t);
  }
  return task.schedule_type || t('harness.unknownSchedule');
}

// The limit a command definition's next fire runs under, in one phrase. Three
// distinct states, and none of them may be rendered as another: a stored positive
// value is the user's own number, a stored 0 is no limit at all, and no stored value
// means the executor's default applies — which is a real limit, so it is named and
// marked as the default rather than left out.
function formatTimeout(
  task: HarnessTask,
  t: (k: string, opts?: Record<string, unknown>) => string,
): string {
  const { seconds, isDefault } = taskTimeout(task);
  if (seconds <= 0) return t('harness.detail.timeoutNone');
  if (isDefault) {
    return t('harness.detail.timeoutDefault', { duration: formatElapsed(seconds, t) });
  }
  return t('harness.detail.timeoutSeconds', { seconds });
}

// Status segments per tab. Definitions filter by what they are doing; runs by
// execution outcome. One control renders whichever set the tab needs.
const RUN_STATUS_FILTERS = ['all', 'queued', 'running', 'succeeded', 'failed', 'canceled'] as const;

// ``default`` hides heartbeats, ``all`` shows every type, anything else is an
// exact run_type match. Kept as one value so the selector has a single source
// of truth for what is on screen.
// 'default', 'all', or a run_type. Deliberately not a union over RUN_TYPES: the
// selector also offers types read back from the ledger that the UI has no
// built-in name for, and those are just as selectable.
type RunTypeFilter = string;

const PAGE_LIMIT = 30;
const EMPTY_DEFINITION_COUNTS: HarnessDefinitionCounts = {
  total: 0,
  running: 0,
  waiting: 0,
  paused: 0,
  finished: 0,
};
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
  const now = useVisibleNow();
  const [tab, setTab] = useState<TabKey>(DEFAULT_TAB);
  const [tasks, setTasks] = useState<HarnessTask[]>([]);
  const [watches, setWatches] = useState<HarnessWatch[]>([]);
  const [runs, setRuns] = useState<HarnessRun[]>([]);
  const [taskCounts, setTaskCounts] = useState<HarnessDefinitionCounts>(EMPTY_DEFINITION_COUNTS);
  const [watchCounts, setWatchCounts] = useState<HarnessDefinitionCounts>(EMPTY_DEFINITION_COUNTS);
  const [runCounts, setRunCounts] = useState<HarnessRunCounts>(EMPTY_RUN_COUNTS);
  const [queryTaskCounts, setQueryTaskCounts] = useState<HarnessDefinitionCounts>(EMPTY_DEFINITION_COUNTS);
  const [queryWatchCounts, setQueryWatchCounts] = useState<HarnessDefinitionCounts>(EMPTY_DEFINITION_COUNTS);
  // Filter-scoped run counts (the tab badge stays global). Same filters feed
  // this and the list, so the "shown / total" hint can never contradict the rows.
  const [queryRunCounts, setQueryRunCounts] = useState<HarnessRunCounts>(EMPTY_RUN_COUNTS);
  const [tasksHasMore, setTasksHasMore] = useState(false);
  const [watchesHasMore, setWatchesHasMore] = useState(false);
  const [runsHasMore, setRunsHasMore] = useState(false);
  // Run types the ledger actually holds, so the selector can offer one that
  // predates or postdates RUN_TYPES instead of stranding those rows under All.
  const [presentRunTypes, setPresentRunTypes] = useState<string[]>([]);
  const [tasksPage, setTasksPage] = useState(1);
  const [watchesPage, setWatchesPage] = useState(1);
  const [runsPage, setRunsPage] = useState(1);
  const [selection, setSelection] = useState<Selection>(null);
  const [selectedRun, setSelectedRun] = useState<HarnessRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [agentsByName, setAgentsByName] = useState<Record<string, VibeAgentBrief>>({});
  // Per-id pending state so the row's toggle / delete buttons can show a
  // spinner without disabling siblings.
  const [pendingMutation, setPendingMutation] = useState<Record<string, boolean>>({});
  const [createKind, setCreateKind] = useState<CreateViaChatKind | null>(null);
  // Search + status filter live on the page so the same controls work
  // for tasks and watches; reset between tab switches happens via
  // setSelection(null) below.
  const [search, setSearch] = useState('');
  // Default to what is actually working right now (waiting + running). The
  // previous default was "enabled", which read a switch as a state: it buried a
  // running task among rows merely left switched on, and swept 1,156 retired
  // one-shots into the same "disabled" bucket as the handful the user paused
  // on purpose. The filtered-count hint keeps the narrowing visible.
  const [statusFilter, setStatusFilter] = useState<HarnessDefinitionStatus>(DEFAULT_DEFINITION_STATUS);
  // Runs filter by outcome, not by enabled/disabled, so they need their own
  // status state. Default "all": a run history with the failures filtered out
  // by default would hide exactly what the user came to look at.
  const [runStatusFilter, setRunStatusFilter] = useState<HarnessRunStatus | 'all'>('all');
  const [runTypeFilter, setRunTypeFilter] = useState<RunTypeFilter>('default');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const refreshSeq = useRef(0);
  const agentRefreshSeq = useRef(0);
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
  const definitionParam = searchParams.get('definition');
  useEffect(() => {
    setTab(harnessTabFromParam(tabParam));
  }, [tabParam]);
  // ?definition=<id> — a run row's trigger chip pointing back at the task/watch
  // that produced it. Seeding the search box (rather than a hidden filter) keeps
  // the narrowing visible and removable; the definition search already matches
  // on id. "all" is required because the originating definition may well be
  // disabled by now. The param is consumed on arrival so clicking the same chip
  // again re-applies it after the user has cleared the box by hand.
  useEffect(() => {
    if (!definitionParam) return;
    setSearch(definitionParam);
    setDebouncedSearch(definitionParam);
    setStatusFilter('all');
    setTasksPage(1);
    setWatchesPage(1);
    // The point of the link is to reveal the definition in the list, so the run
    // detail the user came from has to close. Below `lg`, `hasSelection` hides
    // the list outright — leaving it open means the link shows everything
    // except the thing it promised. The ?run effect below can't cover this: a
    // row click selects without writing ?run, so the param never changes and
    // that effect never re-fires. Clearing here is what makes the chip work
    // from a clicked row, which is how it is actually reached.
    setSelection(null);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete('definition');
        return next;
      },
      { replace: true },
    );
  }, [definitionParam, setSearchParams]);
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
    const query = debouncedSearch || undefined;
    try {
      const result = await api.getHarnessBootstrap({
        tab,
        status: tab === 'runs' ? (runStatusFilter === 'all' ? undefined : runStatusFilter) : statusFilter,
        run_type: tab === 'runs' && runTypeFilter !== 'default' && runTypeFilter !== 'all' ? runTypeFilter : undefined,
        exclude_run_type: tab === 'runs' && runTypeFilter === 'default' ? DEFAULT_HIDDEN_RUN_TYPES : undefined,
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
        setQueryRunCounts(page.counts);
        setRunsHasMore(page.has_more);
        if (page.run_types) setPresentRunTypes(page.run_types);
      }
    } catch (err) {
      if (!isCurrent()) return;
      setError(errorMessage(err) ?? String(err));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [
    api,
    tab,
    debouncedSearch,
    statusFilter,
    runStatusFilter,
    runTypeFilter,
    sessionFilter,
    tasksPage,
    watchesPage,
    runsPage,
  ]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const refreshAgents = useCallback(async () => {
    const seq = agentRefreshSeq.current + 1;
    agentRefreshSeq.current = seq;
    try {
      const agents = await loadHarnessAgentCatalog(api);
      if (agentRefreshSeq.current === seq) setAgentsByName(agents);
    } catch {
      // Harness data remains usable when the optional Agent metadata lookup fails.
    }
  }, [api]);

  useEffect(() => {
    void refreshAgents();
    return () => {
      agentRefreshSeq.current += 1;
    };
  }, [refreshAgents]);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      onRunsUpdated: () => {
        void refresh();
        void refreshAgents();
      },
    });
  }, [api, refresh, refreshAgents]);

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
        if (!definitionSurvivesToggle(statusFilter, next, task)) {
          setSelection((prev) => (prev?.kind === 'task' && prev.id === task.id ? null : prev));
        }
        await refresh();
      } catch (err) {
        setError(errorMessage(err) ?? String(err));
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
      } catch (err) {
        setError(errorMessage(err) ?? String(err));
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
        if (!definitionSurvivesToggle(statusFilter, next, watch)) {
          setSelection((prev) => (prev?.kind === 'watch' && prev.id === watch.id ? null : prev));
        }
        await refresh();
      } catch (err) {
        setError(errorMessage(err) ?? String(err));
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
      } catch (err) {
        setError(errorMessage(err) ?? String(err));
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

  // A tab badge counts what the tab opens on, never more. Tasks and watches
  // open on 进行中, so their badges say how much is live — not how many rows
  // exist, most of which are retired one-shots nobody is waiting on. Runs open
  // unfiltered, so ``all`` already is that tab's default view.
  const counts = useMemo(
    () => ({
      tasks: definitionActiveCount(taskCounts),
      watches: definitionActiveCount(watchCounts),
      runs: runCounts.all,
    }),
    [taskCounts, watchCounts, runCounts.all],
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
  const isRunsTab = tab === 'runs';
  // One filter row, three tabs. Runs swap in outcome statuses and add a type
  // selector; everything else (search, the shown/total hint) is shared.
  const statusOptions: readonly string[] = isRunsTab ? RUN_STATUS_FILTERS : DEFINITION_STATUS_FILTERS;
  const activeStatus: string = isRunsTab ? runStatusFilter : statusFilter;
  const statusLabelPrefix = isRunsTab ? 'harness.runStatus' : 'harness.statusFilter';
  const queryDefinitionCounts = tab === 'tasks' ? queryTaskCounts : queryWatchCounts;
  const totalForTab = isRunsTab ? queryRunCounts.all : queryDefinitionCounts.total;
  // How many rows a chip stands for. A definition chip is a *set* of lifecycle
  // states, so its size is a sum rather than a lookup — ``definitionStatusCount``
  // owns that arithmetic. Every chip carries this (§4.1): a filter row that
  // shows only labels makes the user click each one in turn to learn what the
  // server already told the page.
  const statusCount = useCallback(
    (option: string) =>
      isRunsTab
        ? (queryRunCounts as Record<string, number>)[option] ?? 0
        : definitionStatusCount(queryDefinitionCounts, option),
    [isRunsTab, queryRunCounts, queryDefinitionCounts],
  );
  const shownForTab = statusCount(activeStatus);
  // The shown/total hint only says something when a status narrows the set.
  // The run-type default is stated by the selector itself ("Default (no
  // heartbeats)"), so it needs no second, always-equal "3200/3200" readout.
  const filtersActive = !!search || activeStatus !== 'all';

  const resetPaging = useCallback(() => {
    setTasksPage(1);
    setWatchesPage(1);
    setRunsPage(1);
    setSelection(null);
  }, []);

  const onStatusFilterChange = useCallback(
    (option: string) => {
      if (isRunsTab) setRunStatusFilter(option as HarnessRunStatus | 'all');
      else setStatusFilter(option as HarnessDefinitionStatus);
      resetPaging();
    },
    [isRunsTab, resetPaging],
  );

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
            </button>
          );
        })}
      </div>

      {/* Search + status filter, shared by every list tab. Runs swap the
          lifecycle segments for outcome statuses and add a type selector; all
          three narrow server-side. */}
      <div className="flex flex-wrap items-center gap-2.5">
        <div className="flex h-9 w-full items-center gap-2 rounded-md border border-input bg-background px-3 transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring sm:w-[320px]">
          <Search className="size-3.5 shrink-0 text-muted" />
          <input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              resetPaging();
            }}
            placeholder={t(isRunsTab ? 'harness.searchRunsPlaceholder' : 'harness.searchPlaceholder')}
            className="flex-1 bg-transparent text-[12px] text-foreground outline-none placeholder:text-muted"
          />
        </div>
        <div className="flex rounded-md border border-border-strong bg-surface p-0.5">
          {statusOptions.map((opt) => (
            <button
              key={opt}
              type="button"
              onClick={() => onStatusFilterChange(opt)}
              className={clsx(
                'rounded px-2.5 py-1 text-[11px] font-medium transition',
                activeStatus === opt
                  ? 'bg-violet/[0.12] text-violet'
                  : 'text-muted hover:text-foreground',
              )}
            >
              {t(`${statusLabelPrefix}.${opt}`)}
              <span
                className={clsx(
                  'ml-1 tabular-nums',
                  activeStatus === opt ? 'text-violet/70' : 'text-muted/70',
                )}
              >
                {statusCount(opt)}
              </span>
            </button>
          ))}
        </div>
        {isRunsTab && (
          // Heartbeat rows are hidden by default (plan D1). The selector is
          // where that default is stated and undone — it must never read as
          // an unfiltered list that happens to be short.
          <select
            value={runTypeFilter}
            onChange={(e) => {
              setRunTypeFilter(e.target.value as RunTypeFilter);
              resetPaging();
            }}
            aria-label={t('harness.runTypeFilter.label')}
            className="h-9 rounded-md border border-input bg-background px-2.5 text-[11px] font-medium text-foreground outline-none transition-colors focus:border-ring focus:ring-2 focus:ring-ring"
          >
            <option value="default">{t('harness.runTypeFilter.default')}</option>
            <option value="all">{t('harness.runTypeFilter.all')}</option>
            {runTypeOptions(presentRunTypes).map((type) => (
              <option key={type} value={type}>
                {runTypeLabel(type, t)}
              </option>
            ))}
          </select>
        )}
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
        {filtersActive && (
          <span className="ml-auto font-mono text-[10px] text-muted">
            {t('harness.filtered', { shown: shownForTab, total: totalForTab })}
          </span>
        )}
      </div>

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
              hasStoredRows={taskCounts.total > 0}
              selectedId={selection?.kind === 'task' ? selection.id : null}
              onSelect={(id) => setSelection({ kind: 'task', id })}
              onToggleEnabled={toggleTaskEnabled}
              onDelete={deleteTask}
              pending={pendingMutation}
              now={now}
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
              hasStoredRows={watchCounts.total > 0}
              selectedId={selection?.kind === 'watch' ? selection.id : null}
              onSelect={(id) => setSelection({ kind: 'watch', id })}
              onToggleEnabled={toggleWatchEnabled}
              onDelete={deleteWatch}
              pending={pendingMutation}
              now={now}
              page={watchesPage}
              hasMore={watchesHasMore}
              onPageChange={(page) => {
                setWatchesPage(page);
                setSelection(null);
              }}
            />
          )}
          {tab === 'runs' && (
            <RunsList
              runs={runs}
              agentsByName={agentsByName}
              loading={loading}
              hasStoredRows={runCounts.all > 0}
              selectedId={selection?.kind === 'run' ? selection.id : null}
              onSelect={(id) => setSelection({ kind: 'run', id })}
              now={now}
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
              <RunDetail run={selectedRun} agent={agentsByName[selectedRun.agent_name ?? '']} />
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
  hasStoredRows: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onToggleEnabled: (task: HarnessTask) => void;
  onDelete: (task: HarnessTask) => void;
  pending: Record<string, boolean>;
  now: number;
  page: number;
  hasMore: boolean;
  onPageChange: (page: number) => void;
}

const TasksList: React.FC<TasksListProps> = ({
  tasks,
  loading,
  hasStoredRows,
  selectedId,
  onSelect,
  onToggleEnabled,
  onDelete,
  pending,
  now,
  page,
  hasMore,
  onPageChange,
}) => {
  const { t } = useTranslation();
  if (tasks.length === 0 && !loading) return <EmptyState i18nKey={harnessEmptyStateKey('tasks', hasStoredRows)} />;
  return (
    <>
      {tasks.map((task) => (
        <DefinitionRow
          key={task.id}
          row={task}
          kind="task"
          active={selectedId === task.id}
          pending={!!pending[task.id]}
          now={now}
          onSelect={() => onSelect(task.id)}
          onToggle={() => onToggleEnabled(task)}
          onDelete={() => onDelete(task)}
        />
      ))}
      {tasks.length === 0 && loading && <div className="px-4 py-6 text-[12px] text-muted">{t('common.loading')}</div>}
      <HarnessPager page={page} hasMore={hasMore} onPageChange={onPageChange} />
    </>
  );
};

// One row shape for both definition tabs: a task and a watch differ in what
// their second line *says*, never in how the row is built. ``definitionRowLine``
// owns that difference so this component stays a single anatomy.
const STATE_DOT_CLASS: Record<HarnessLifecycleState, string> = {
  running: 'bg-mint shadow-[0_0_5px_rgba(52,211,153,0.7)]',
  waiting: 'bg-cyan',
  paused: 'bg-muted/70',
  finished: 'bg-border-strong',
};

// A state this client has no colour for still gets a dot — the row keeps its
// shape, and the second line names the state in words either way.
function stateDotClass(state: HarnessLifecycleState | null): string {
  return (state && STATE_DOT_CLASS[state]) || 'bg-muted/70';
}

const ALERT_CLASS: Record<HarnessRowAlert, string> = {
  error: 'text-pink',
  timeout: 'text-amber',
  // A waiter that is supposed to be waiting but whose process is gone. It is
  // not an error the store recorded — it is the absence of one, which is
  // exactly why nothing used to show it.
  dead: 'text-pink',
  // Recovered but not clean: the newest verdict succeeded while a failure is
  // still in the window. Amber rather than pink — it is a "look at this", not a
  // "this is broken right now".
  degraded: 'text-amber',
  // Health could not be read at all. Muted, because the fault is in the
  // reporting path rather than in the definition — but present, because the
  // alternative is a row that looks like it passed.
  unknown: 'text-muted',
};

// Derived health, on the LIST row rather than only in the detail pane. A cron
// task failing every night used to render identically to one succeeding every
// night: the only failure signal was ``last_error``, which lives behind a click,
// and the row's alert channel was driven by ``lifecycle_detail`` — null unless the
// row is ``finished``, which a recurring definition never is.
//
// ``healthy`` renders nothing — a badge on every passing row is noise, and the
// silence is what makes the other three worth looking at.
//
// ``unknown`` does render. The server emits it when the health read failed or
// the stored metadata was malformed, so it is a rare fault state, not a noisy
// one; rendering it as nothing produced a spotless Harness list at precisely the
// moment the failure signal could not be computed, which is the opposite of what
// the projection contract promises. Muted rather than pink or amber, because
// what is broken is the reporting path, not necessarily the definition.
export const HealthBadge: React.FC<{ row: HarnessTask | HarnessWatch }> = ({ row }) => {
  const { t } = useTranslation();
  const health = definitionHealth(row);
  if (health !== 'failing' && health !== 'degraded' && health !== 'unknown') return null;
  const failureSummaryKey = definitionFailureSummaryKey(row);
  // No count on ``unknown``: both counters come from the same run history this
  // row could not read, so printing one would put a number on nothing.
  const count = health === 'unknown' ? 0 : health === 'failing' ? row.consecutive_failures : row.recent_failures;
  return (
    <Badge
      variant="secondary"
      className={clsx(
        'shrink-0 font-mono text-[9px] uppercase',
        health === 'failing' ? 'text-pink' : health === 'degraded' ? 'text-amber' : 'text-muted',
      )}
      title={failureSummaryKey ? t(failureSummaryKey) : undefined}
    >
      {t(`harness.health.${health}`)}
      {count > 1 ? ` ${count}` : ''}
    </Badge>
  );
};

const visibleProcessingHealth = (row: HarnessWatch) => {
  const health = definitionProcessingHealth(row);
  return health === 'failing' || health === 'degraded' || health === 'unknown' ? health : null;
};

export const ProcessingHealthBadge: React.FC<{ row: HarnessWatch }> = ({ row }) => {
  const { t } = useTranslation();
  const health = visibleProcessingHealth(row);
  if (!health) return null;
  const count =
    health === 'unknown'
      ? 0
      : health === 'failing'
        ? row.processing_consecutive_failures || 0
        : row.processing_recent_failures || 0;
  return (
    <Badge
      variant="secondary"
      className={clsx(
        'shrink-0 font-mono text-[9px] uppercase',
        health === 'failing' ? 'text-pink' : health === 'degraded' ? 'text-amber' : 'text-muted',
      )}
    >
      {t(`harness.processingHealth.${health}`)}
      {count > 1 ? ` ${count}` : ''}
    </Badge>
  );
};

// What a task *does*, when that is not the default. A command task runs a
// subprocess instead of prompting an Agent, and nothing else on the row says so:
// the schedule chip, the state dot and the second line read identically for both
// kinds, so an operator scanning the list had no way to tell a shell task from a
// message task without opening it.
//
// A message task gets no chip — the same philosophy as ``HealthBadge``'s
// ``healthy``: the overwhelming majority are message tasks, a chip on every one
// of them is noise, and the silence is what makes this chip worth reading.
// Watches are excluded outright: every watch runs a command, so the chip would
// say nothing there (their ``kind`` chip already reads once/continuous).
export const TaskKindBadge: React.FC<{ row: HarnessTask | HarnessWatch; kind: HarnessDefinitionKind }> = ({
  row,
  kind,
}) => {
  const { t } = useTranslation();
  if (kind !== 'task' || !taskIsCommand(row)) return null;
  return (
    <Badge variant="secondary" className="shrink-0 font-mono text-[9px] uppercase" title={taskCommandPreview(row)}>
      {t('harness.taskKind.command')}
    </Badge>
  );
};

interface DefinitionRowProps {
  row: HarnessTask | HarnessWatch;
  kind: HarnessDefinitionKind;
  active: boolean;
  pending: boolean;
  now: number;
  onSelect: () => void;
  onToggle: () => void;
  onDelete: () => void;
}

const DefinitionRow: React.FC<DefinitionRowProps> = ({
  row,
  kind,
  active,
  pending,
  now,
  onSelect,
  onToggle,
  onDelete,
}) => {
  const { t } = useTranslation();
  // A command task has no name and no message to fall back to — its ``prompt``
  // is empty by construction — so the existing chain landed on the kind label
  // and every unnamed command task in the list rendered as the word "Task".
  // The command is what identifies it, exactly as ``_watch_display_name`` uses
  // it for a watch; it goes in the last slot of the same chain rather than
  // ahead of the user's own name.
  const title = definitionRowTitle(
    row,
    kind === 'task' && taskIsCommand(row) ? taskCommandPreview(row) : t(`harness.kind.${kind}`),
  );
  const chip = definitionChipLabel(row, kind, t);
  const line = definitionRowLine(row, kind, t, now);
  return (
    <div
      className={clsx(
        'group/row flex min-w-0 items-center gap-3 rounded-lg border px-4 py-3 transition',
        active ? 'border-violet/40 bg-violet/[0.05]' : 'border-border bg-surface hover:bg-foreground/[0.03]',
      )}
    >
      <button type="button" onClick={onSelect} className="flex min-w-0 flex-1 items-center gap-3 text-left">
        <span
          aria-hidden
          className={clsx('size-2 shrink-0 rounded-full', stateDotClass(row.lifecycle_state))}
        />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex min-w-0 items-center gap-2">
            {/* Truncation is CSS, never a slice: search matches the full
                title and the detail panel shows it whole. */}
            <span className="truncate text-[14px] font-semibold text-foreground" title={title}>
              {title}
            </span>
            <TaskKindBadge row={row} kind={kind} />
            {chip && (
              <Badge variant="secondary" className="shrink-0 font-mono text-[9px] uppercase">
                {chip}
              </Badge>
            )}
            <HealthBadge row={row} />
            {kind === 'watch' && <ProcessingHealthBadge row={row as HarnessWatch} />}
          </div>
          <div className="flex min-w-0 items-center gap-2 text-[11px] text-muted">
            {line.alert && <AlertTriangle className={clsx('size-3 shrink-0', ALERT_CLASS[line.alert])} />}
            <span className={clsx('truncate', line.alert && ALERT_CLASS[line.alert])}>{line.primary}</span>
            {line.secondary && (
              <span className="shrink-0 truncate font-mono text-[10px] text-muted">· {line.secondary}</span>
            )}
          </div>
        </div>
      </button>
      <RowActions enabled={row.enabled} pending={pending} onToggle={onToggle} onDelete={onDelete} />
    </div>
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

/** What to print for a command task that stores no working directory of its own.
 *
 * Names the CHAIN the fire actually walks (``_execute_command_task``) -- the bound
 * Session's workdir read live, then ``runtime.default_cwd``, then the Avibe state
 * directory -- rather than an outcome the pane cannot verify. "Session working
 * directory" was the wrong promise: ``_bound_session_workdir`` answers ``None`` for a
 * deleted row, a NULL workdir or a failed read, and every fallback is
 * ``isdir``-validated besides, so the command can land further down the chain while the
 * pane names a Session. The last term is not decoration either -- ``default_cwd`` is
 * unset on a fresh install and revalidated on every fire, so "Runtime default" alone
 * named a specific config key that the run may never reach.
 *
 * Where the pane can already see the first term is impossible -- no binding at all, or
 * one whose Session row is gone (the same ``deleted`` state the Session field prints
 * two rows up) -- it drops that term instead of contradicting itself.
 */
function commandCwdFallbackKey(task: HarnessTask): string {
  const state = harnessSessionState(task, task.session_id);
  return state === 'none' || state === 'deleted'
    ? 'harness.detail.cwdRuntimeDefault'
    : 'harness.detail.cwdFromSession';
}

interface TaskDetailProps {
  task: HarnessTask;
  agent?: VibeAgentBrief;
  onToggleEnabled: () => void;
  pending: boolean;
}

/** Agent, Session, session mode, delivery and the message they carry.
 *
 * One component because they are one answer -- who runs this, where it lands -- and
 * because a command task needs that answer said differently: it routes only a FAILURE,
 * and its message is triage guidance rather than the payload of every run. Both
 * differences are wording and framing, so they are props here instead of a second copy
 * of the fields.
 */
const RoutingFields: React.FC<{
  task: HarnessTask;
  agent?: VibeAgentBrief;
  group?: string;
  messageLabel: string;
  showMessage: boolean;
}> = ({ task, agent, group, messageLabel, showMessage }) => {
  const { t } = useTranslation();
  const fields = (
    <>
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
      {showMessage && (
        <DetailField label={messageLabel}>
          <pre className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[11px] text-foreground">
            {task.message || task.prompt || '—'}
          </pre>
        </DetailField>
      )}
    </>
  );
  return group ? <DetailGroup label={group}>{fields}</DetailGroup> : fields;
};

export const TaskDetail: React.FC<TaskDetailProps> = ({ task, agent, onToggleEnabled, pending }) => {
  const { t } = useTranslation();
  const isCommand = taskIsCommand(task);
  const commandPreview = isCommand ? taskCommandPreview(task) : '';
  const escalates = isCommand && taskOnFailure(task) === 'agent';
  const routesSomewhere =
    !isCommand ||
    taskOnFailure(task) === 'agent' ||
    Boolean(task.agent_name) ||
    Boolean(task.session_id) ||
    Boolean(task.session_key) ||
    Boolean(task.post_to) ||
    Boolean(task.deliver_key);
  const title = definitionRowTitle(task, isCommand ? commandPreview : t('harness.kind.task'));
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex min-w-0 items-center gap-2">
        <Calendar className="size-4 shrink-0 text-violet" />
        <div className="min-w-0 flex-1 truncate text-[15px] font-bold text-foreground" title={title}>
          {title}
        </div>
        <LifecyclePill row={task} />
        <Switch
          checked={task.enabled}
          onCheckedChange={onToggleEnabled}
          label={t(task.enabled ? 'harness.row.disable' : 'harness.row.enable')}
          disabled={pending}
        />
      </div>
      {/* Same anatomy and position as ``WatchDetail``'s command field — the two
          run a subprocess the same way, so they read the same way. The preview
          truncates for the header; this is the copyable full text. */}
      {isCommand && (
        <DetailField label={t('harness.detail.command')}>
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[11px] text-foreground">
            {formatCommandLine(task.shell_command, task.command) || '—'}
          </pre>
        </DetailField>
      )}
      {/* Where the command runs and how long it may take -- process mechanics, so they
          sit with the command rather than with the failure lane below. ``WatchDetail``
          has shown the working directory since it shipped; the task pane never did, so
          a scheduled command's directory was unanswerable from the UI even once
          ``--cwd`` stored one. A null is not "nowhere": the fire walks a chain, so the
          field names the chain rather than printing the em-dash a missing field would.
          */}
      {isCommand && (
        <div className="grid grid-cols-2 gap-4">
          <DetailField label={t('harness.detail.cwd')}>
            <code className="font-mono text-[11px] text-muted">
              {task.cwd || t(commandCwdFallbackKey(task))}
            </code>
          </DetailField>
          <DetailField label={t('harness.detail.timeout')}>
            {/* Always rendered, because a limit is always in force: a row with no
                stored timeout is run under the six-hour default, and hiding the
                field read as "no limit". A stored 0 IS "no limit" server-side, and
                printing "0s" would read as an instant kill — the opposite. The
                default is shown in duration words ("6h") because it is a policy
                constant nobody typed; a stored value is echoed in the seconds the
                user actually set. */}
            <span className="font-mono text-[11px] text-muted">{formatTimeout(task, t)}</span>
          </DetailField>
        </div>
      )}
      {/* The row humanizes the schedule; this is where the literal lives, so
          an operator can still read and copy the exact expression. */}
      <DetailField label={t('harness.detail.schedule')}>
        <span className="text-[12px] text-foreground">{formatSchedule(task, t)}</span>
        {(task.cron || task.run_at) && (
          <span className="ml-2 font-mono text-[10px] text-muted">{task.cron ?? task.run_at}</span>
        )}
        {task.timezone && <span className="ml-2 text-[10px] text-muted">{task.timezone}</span>}
      </DetailField>
      {task.next_run_at && (
        <DetailField label={t('harness.detail.nextRun')}>
          <span className="text-[12px] text-foreground">{humanizeTime(task.next_run_at, t)}</span>
          <span className="ml-2 font-mono text-[10px] text-muted">
            {formatLocalDateTime(task.next_run_at)}
          </span>
        </DetailField>
      )}
      {/* Routing, and only where there is something to route. These three label
          helpers all resolve a null to a CONCRETE answer — "Inherited default",
          "Existing", "Session" — which is right for a message task, whose null
          agent really is the inherited default. A command created with the CLI's
          default ``--on-failure none`` is bound to nothing at all, so the same
          rendering told the user it routes through an Agent it deliberately does
          not have. Shown as soon as anything here is real: an escalation turn
          (``--on-failure agent``), a pinned Agent, or a conversation a failure
          notice is delivered to. */}
      {/* BEFORE the routing it governs, not after it. "no Agent" is a deliberate
          configuration rather than an omission, so it is stated rather than left
          blank — and when it says the opposite, it is the sentence that makes the
          block below mean "only when this fails". */}
      {isCommand && (
        <DetailField label={t('harness.detail.onFailure')}>
          <span className="text-[12px] text-foreground">{t(`harness.onFailure.${taskOnFailure(task)}`)}</span>
        </DetailField>
      )}
      {routesSomewhere && (
        <RoutingFields
          task={task}
          agent={agent}
          // Only an ESCALATING command task is describing a path it does not take on a
          // healthy day. A message task routes every run through these same fields, so
          // grouping them under a failure heading there would be a lie in the other
          // direction -- and so would grouping them for a command task whose
          // ``on_failure`` is ``none``. That row is exactly what SCT-043's gate keeps:
          // no Agent turn, but a real Session or delivery target carrying the failure
          // notice. Keyed on the same value the field above prints, so the pane cannot
          // say "Notice only (no Agent)" and "escalation" about one task.
          group={escalates ? t('harness.detail.escalation') : undefined}
          messageLabel={escalates ? t('harness.detail.triagePrompt') : t('harness.detail.message')}
          showMessage={Boolean(!isCommand || task.message || task.prompt)}
        />
      )}
      {/* A command task's ``prompt`` is empty by construction, so this field
          would render a bare em-dash under the heading "Message" — a promise of
          a message the task does not have. A command task that *also* carries
          one (``--on-failure agent``) shows it above, inside the escalation
          group, because that is the only thing it is. This is the leftover case:
          a stored message with nothing to route it, which no current CLI path
          creates and old rows still can. Shown rather than dropped. */}
      {!routesSomewhere && (task.message || task.prompt) && (
        <DetailField label={t('harness.detail.message')}>
          <pre className="max-h-44 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[11px] text-foreground">
            {task.message || task.prompt || '—'}
          </pre>
        </DetailField>
      )}
      {task.last_run_at && (
        <DetailField label={t('harness.detail.lastRun')}>
          <span className="font-mono text-[11px] text-muted">{formatLocalDateTime(task.last_run_at)}</span>
        </DetailField>
      )}
      {/* The verdict of the last command run, on the definition rather than only
          on the run row: a nightly command task's exit code is the one fact an
          operator wants without paging through the runs tab. Not gated on
          ``last_run_at`` — a stored exit code proves a run happened. */}
      <FailureDetails row={task} />
      {task.last_exit_code != null && (
        <DetailField label={t('harness.detail.lastExitCode')}>
          <span
            className={clsx(
              'font-mono text-[11px]',
              definitionExitCodeTone(task) === 'failure' ? 'text-pink' : 'text-muted',
            )}
          >
            {task.last_exit_code}
          </span>
        </DetailField>
      )}
      {task.resume_blocked?.code === 'task_owner_session_unavailable' && (
        <DetailField label={t('harness.detail.pauseReason')}>
          <span className="text-[12px] text-muted">
            {t('harness.taskPauseReason.ownerSessionUnavailable')}
          </span>
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
  hasStoredRows: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onToggleEnabled: (watch: HarnessWatch) => void;
  onDelete: (watch: HarnessWatch) => void;
  pending: Record<string, boolean>;
  now: number;
  page: number;
  hasMore: boolean;
  onPageChange: (page: number) => void;
}

const WatchesList: React.FC<WatchesListProps> = ({
  watches,
  loading,
  hasStoredRows,
  selectedId,
  onSelect,
  onToggleEnabled,
  onDelete,
  pending,
  now,
  page,
  hasMore,
  onPageChange,
}) => {
  const { t } = useTranslation();
  if (watches.length === 0 && !loading) {
    return <EmptyState i18nKey={harnessEmptyStateKey('watches', hasStoredRows)} />;
  }
  return (
    <>
      {watches.map((watch) => (
        <DefinitionRow
          key={watch.id}
          row={watch}
          kind="watch"
          active={selectedId === watch.id}
          pending={!!pending[watch.id]}
          now={now}
          onSelect={() => onSelect(watch.id)}
          onToggle={() => onToggleEnabled(watch)}
          onDelete={() => onDelete(watch)}
        />
      ))}
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

export const WatchDetail: React.FC<WatchDetailProps> = ({ watch, agent, onToggleEnabled, pending }) => {
  const { t } = useTranslation();
  const cmd = formatCommandLine(watch.shell_command, watch.command) || '—';
  const title = definitionRowTitle(watch, t('harness.kind.watch'));
  const showRuntime =
    watch.process_alive === true || (watch.process_alive === false && waiterExpectedAlive(watch));
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex min-w-0 items-center gap-2">
        <Eye className="size-4 shrink-0 text-violet" />
        <div className="min-w-0 flex-1 truncate text-[15px] font-bold text-foreground" title={title}>
          {title}
        </div>
        <LifecyclePill row={watch} />
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
      {watch.waiting_since && (
        <DetailField label={t('harness.detail.waitingSince')}>
          <span className="text-[12px] text-foreground">{humanizeTime(watch.waiting_since, t)}</span>
          <span className="ml-2 font-mono text-[10px] text-muted">
            {formatLocalDateTime(watch.waiting_since)}
          </span>
        </DetailField>
      )}
      {/* A live process remains useful diagnostics. A stopped process is news
          only while the shared predicate says this waiter should be running. */}
      {showRuntime && (
        <DetailField label={t('harness.detail.runtime')}>
          <span
            className={clsx('text-[12px]', watch.process_alive ? 'text-foreground' : 'text-pink')}
          >
            {t(watch.process_alive ? 'harness.row.processAlive' : 'harness.row.processDead')}
          </span>
          {watch.runtime.pid != null && (
            <span className="ml-2 font-mono text-[10px] text-muted">
              pid {watch.runtime.pid}
              {watch.runtime.started_at && ` · ${formatLocalDateTime(watch.runtime.started_at)}`}
            </span>
          )}
        </DetailField>
      )}
      <FailureDetails row={watch} />
      {watch.last_exit_code != null && (
        <DetailField label={t('harness.detail.lastExitCode')}>
          <span
            className={clsx(
              'font-mono text-[11px]',
              definitionExitCodeTone(watch) === 'failure' ? 'text-pink' : 'text-muted',
            )}
          >
            {watch.last_exit_code}
          </span>
        </DetailField>
      )}
      {visibleProcessingHealth(watch) && (
        <DetailField label={t('harness.detail.eventProcessing')}>
          <ProcessingHealthBadge row={watch} />
        </DetailField>
      )}
      <DetailField label={t('harness.detail.id')}>
        <code className="font-mono text-[11px] text-muted">{watch.id}</code>
      </DetailField>
    </div>
  );
};

// ---------------------------------------------------------------------------
// Runs tab
// ---------------------------------------------------------------------------

interface RunsListProps {
  runs: HarnessRun[];
  agentsByName: Record<string, VibeAgentBrief>;
  loading: boolean;
  hasStoredRows: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  now: number;
  page: number;
  hasMore: boolean;
  onPageChange: (page: number) => void;
}

const RunsList: React.FC<RunsListProps> = ({
  runs,
  agentsByName,
  loading,
  hasStoredRows,
  selectedId,
  onSelect,
  now,
  page,
  hasMore,
  onPageChange,
}) => {
  const { t } = useTranslation();
  if (runs.length === 0 && !loading) return <EmptyState i18nKey={harnessEmptyStateKey('runs', hasStoredRows)} />;
  return (
    <>
      {runs.map((run) => {
        const active = selectedId === run.id;
        const typeLabel = runTypeLabel(run.run_type, t);
        const title = runRowTitle(run, typeLabel);
        const elapsed = runElapsedSeconds(run, now);
        // The whole row selects the run, but the trigger chip is its own link,
        // so the row can't be a <button> (no interactive descendants). An
        // absolutely-positioned overlay button takes the row click instead, and
        // the content opts out of pointer events except where it links.
        return (
          <div
            key={run.id}
            className={clsx(
              'relative rounded-lg border px-4 py-3 transition',
              active ? 'border-violet/40 bg-violet/[0.05]' : 'border-border bg-surface hover:bg-foreground/[0.03]',
            )}
          >
            <button
              type="button"
              onClick={() => onSelect(run.id)}
              aria-label={title}
              className="absolute inset-0 z-0 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            <div className="pointer-events-none relative z-10 flex items-start gap-3">
              <span className="mt-0.5">
                <RunStatusIcon status={run.status} />
              </span>
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex min-w-0 items-center gap-2">
                  {/* CSS truncation, never a slice: the detail panel and the
                      server-side search keep the full message. */}
                  <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-foreground">{title}</span>
                  <span className="shrink-0 rounded border border-border-strong bg-foreground/[0.04] px-1.5 py-0 text-[9px] font-medium uppercase tracking-wide text-muted">
                    {typeLabel}
                  </span>
                </div>
                <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 text-[11px] text-muted">
                  <RunTriggerChip run={run} />
                  {run.agent_name && (
                    <span className="inline-flex min-w-0 items-center gap-1">
                      <Bot className="size-3 shrink-0" />
                      <span className="truncate">
                        {agentDisplayName(run.agent_name, agentsByName[run.agent_name])}
                      </span>
                    </span>
                  )}
                  <RunSessionLabel run={run} />
                  {elapsed != null && <span className="shrink-0 font-mono">{formatElapsed(elapsed, t)}</span>}
                  {run.created_at && (
                    <span className="shrink-0">{formatRelativeTime(run.created_at, t, now)}</span>
                  )}
                </div>
              </div>
            </div>
          </div>
        );
      })}
      <HarnessPager page={page} hasMore={hasMore} onPageChange={onPageChange} />
    </>
  );
};

// The task/watch a run came from, named. Links back to it unless the
// definition was deleted — a run outlives its definition, and the name is still
// worth showing even when there is nothing left to open.
export const RunTriggerChip: React.FC<{ run: HarnessRun }> = ({ run }) => {
  const { t } = useTranslation();
  if (!run.definition_name && !run.definition_id) return null;
  const label = run.definition_name || run.definition_id || '';
  const linkable = !!run.definition_kind && !run.definition_deleted && !!run.definition_id;
  const body = (
    <>
      {run.definition_kind === 'watch' ? (
        <Eye className="size-3 shrink-0" />
      ) : (
        <Calendar className="size-3 shrink-0" />
      )}
      <span className="max-w-[180px] truncate">{label}</span>
      {run.definition_deleted && <span className="shrink-0">{t('harness.run.definitionDeleted')}</span>}
    </>
  );
  if (!linkable) return <span className="inline-flex min-w-0 items-center gap-1">{body}</span>;
  return (
    <Link
      to={`/harness?tab=${run.definition_kind === 'watch' ? 'watches' : 'tasks'}&definition=${encodeURIComponent(run.definition_id!)}`}
      onClick={(e) => e.stopPropagation()}
      className="pointer-events-auto inline-flex min-w-0 items-center gap-1 text-violet hover:underline"
    >
      {body}
    </Link>
  );
};

// Compact session label for a run row. The row is for scanning; the actionable
// chat link lives in the detail panel's DetailSession.
const RunSessionLabel: React.FC<{ run: HarnessRun }> = ({ run }) => {
  const { t } = useTranslation();
  const state = harnessSessionState(run, run.session_id);
  if (state === 'none') return null;
  if (state === 'deleted') {
    return (
      <span className="inline-flex min-w-0 items-center gap-1 text-muted/70">
        <MessageSquareOff className="size-3 shrink-0" />
        <span className="truncate">{t('harness.detail.sessionDeleted')}</span>
      </span>
    );
  }
  return (
    <span className="inline-flex min-w-0 items-center gap-1">
      {state === 'workbench' ? (
        <MessageSquare className="size-3 shrink-0" />
      ) : (
        run.session_platform && <PlatformIcon platform={run.session_platform} size={12} />
      )}
      <span className="max-w-[200px] truncate">{run.session_label || run.session_title || '—'}</span>
    </span>
  );
};

interface RunDetailProps {
  run: HarnessRun;
  agent?: VibeAgentBrief;
}

export const RunDetail: React.FC<RunDetailProps> = ({ run, agent }) => {
  const { t } = useTranslation();
  const typeLabel = runTypeLabel(run.run_type || run.request_type, t);
  const title = runRowTitle(run, typeLabel);
  const runCommandLine = runCommandSnapshotLine(run);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start gap-2">
        <span className="mt-0.5">
          <RunStatusIcon status={run.status} />
        </span>
        <span
          className="line-clamp-2 min-w-0 flex-1 break-words text-[13px] font-semibold text-foreground"
          title={title}
        >
          {title}
        </span>
        <span
          className={clsx(
            'shrink-0 rounded border px-2 py-0 text-[9px] font-bold uppercase tracking-wide',
            STATUS_PILL_CLASS[run.status as HarnessRunStatus] ?? 'border-border-strong bg-foreground/[0.04] text-muted',
          )}
        >
          {runStatusLabel(run.status, t)}
        </span>
      </div>
      {/* The run id left the row headline (plan §4.1) — it lives here, one
          click from the clipboard, for the ``vibe runs show <id>`` handoff. */}
      <DetailField label={t('harness.detail.id')}>
        <code className="block select-all break-all font-mono text-[11px] text-muted">{run.id}</code>
      </DetailField>
      <DetailField label={t('harness.detail.type')}>
        <span className="text-[12px] text-foreground">{typeLabel}</span>
      </DetailField>
      <DetailField label={t('harness.detail.agent')}>
        <span className="text-[12px] text-foreground">{agentDisplayName(run.agent_name, agent)}</span>
        {run.agent_backend && <span className="ml-2 font-mono text-[10px] text-muted">{run.agent_backend}</span>}
        {run.model && <span className="ml-2 font-mono text-[10px] text-muted">{run.model}</span>}
      </DetailField>
      {(run.definition_name || run.definition_id) && (
        <DetailField label={t('harness.detail.triggeredBy')}>
          <div className="flex min-w-0 items-center text-[12px] text-muted">
            <RunTriggerChip run={run} />
          </div>
        </DetailField>
      )}
      {/* Session + lineage (Part B): the run row already carries these; surface
          them instead of hiding the who-started-whom / where-it-reports story.
          DetailSession owns the four session states, so a deleted session says
          so and an IM session stays deliberately unlinked. */}
      <DetailField label={t('harness.detail.session')}>
        <DetailSession summary={run} sessionId={run.session_id} />
      </DetailField>
      {(run.source_kind || run.source_actor) && (
        <DetailField label={t('harness.detail.source')}>
          <span className="inline-flex min-w-0 flex-wrap items-center gap-1.5 text-[12px] text-foreground">
            {run.source_kind && (
              <span className="rounded border border-border-strong bg-foreground/[0.04] px-1.5 py-0 font-mono text-[10px] uppercase text-muted">
                {run.source_kind}
              </span>
            )}
            {/* ``source_actor`` is a session id exactly when another agent
                spawned this run; the projection resolves that case, so the
                field says who — not ``ses53w9zb8ba6``. Every other kind
                (parent run, vault request, activity) has no name to look up
                and stays the plain string it is. */}
            {run.source_session ? (
              <DetailSession summary={run.source_session} sessionId={run.source_session_id} />
            ) : (
              run.source_actor && (
                <span className="font-mono text-[11px] text-muted">{run.source_actor}</span>
              )
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
          <div className="flex min-w-0 flex-col gap-1">
            {/* Where the result reports back to — the same four session states,
                so "who gets told" is as openable as "who ran it". */}
            <DetailSession
              summary={run.callback_session ?? BLANK_SESSION_SUMMARY}
              sessionId={run.callback_session_id}
            />
            {run.callback_status && (
              <span className="w-fit rounded border border-border-strong bg-foreground/[0.04] px-1.5 py-0 font-mono text-[10px] uppercase text-muted">
                {run.callback_status}
              </span>
            )}
          </div>
          {run.callback_error && (
            <div className="mt-1 rounded-md border border-destructive/40 bg-destructive/[0.06] px-2 py-1 text-[11px] text-destructive">
              {run.callback_error}
            </div>
          )}
        </DetailField>
      )}
      {/* What this run actually executed, read off the run's own snapshot. The
          definition it came from is editable and deletable, so it cannot answer for a
          past execution — only the run can. */}
      {runCommandLine && (
        <DetailField label={t('harness.detail.command')}>
          <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-surface-3 p-2 font-mono text-[11px] text-foreground">
            {runCommandLine}
          </pre>
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

// The detail-panel counterpart of the row's state dot. Same four words as the
// filter chips, so a row found under 已结束 says 已结束 when opened.
const LifecyclePill: React.FC<{ row: HarnessTask | HarnessWatch }> = ({ row }) => {
  const { t } = useTranslation();
  const state = row.lifecycle_state;
  return (
    <Badge
      variant={state === 'running' ? 'success' : 'secondary'}
      className="shrink-0 font-mono text-[9px] uppercase"
    >
      {state === 'running' ? (
        <span className="size-1.5 rounded-full bg-mint" />
      ) : state === 'paused' ? (
        <PauseCircle className="size-2.5" />
      ) : null}
      {lifecycleLabel(state, row.lifecycle_detail, t)}
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
// global default); model/effort can be null in legacy or partial records.
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
      <span className="shrink-0 text-[12px] font-medium text-foreground">{agent?.display_name || agentName}</span>
      {meta && <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted">{meta}</span>}
      {!agent?.archived && (
        <Link
          // This opens the agent's definition, so it asks for the Definitions tab
          // explicitly rather than resuming whichever tab was left on.
          to="/agents?tab=definitions"
          className="ml-auto inline-flex shrink-0 items-center gap-0.5 text-[11px] font-medium text-violet hover:underline"
        >
          {t('harness.detail.openInAgents')}
          <ArrowUpRight className="size-3" />
        </Link>
      )}
    </div>
  );
};

// Bound session, in the four states a harness row can be in. A session id that
// no longer resolves says so instead of rendering a raw id that looks like a
// name.
//
// Two independent questions, deliberately answered by two fields:
//   - *how it reads* — ``session_is_workbench`` picks the icon and whether the
//     label is the session title or the IM channel;
//   - *whether it opens* — ``session_openable`` alone, the one predicate the
//     Agents graph and this page now share
//     (``storage/agent_session_rows.py::session_openable_in_chat``).
// They used to be the same field, which is why an IM-bound task rendered a
// title the user could see but not click, even though ``/chat/<id>`` serves it.
export const DetailSession: React.FC<{ summary: HarnessSessionSummary; sessionId: string | null }> = ({
  summary,
  sessionId,
}) => {
  const { t } = useTranslation();
  const state = harnessSessionState(summary, sessionId);
  if (state === 'none') {
    return <span className="text-[12px] text-muted">{t('harness.detail.sessionNone')}</span>;
  }
  if (state === 'deleted') {
    return (
      <div className="flex min-w-0 items-center gap-2">
        <MessageSquareOff className="size-3.5 shrink-0 text-muted" />
        <span className="shrink-0 text-[12px] text-muted">{t('harness.detail.sessionDeleted')}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted/70">{sessionId}</span>
      </div>
    );
  }
  const body =
    state === 'workbench' ? (
      <>
        <MessageSquare className="size-3.5 shrink-0 text-cyan" />
        <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-foreground">
          {summary.session_title || sessionId || '—'}
        </span>
      </>
    ) : (
      <>
        {summary.session_platform && <PlatformIcon platform={summary.session_platform} size={14} />}
        <span className="min-w-0 flex-1 truncate text-[12px] font-medium text-foreground">
          {summary.session_label || summary.session_title || sessionId || '—'}
        </span>
        {summary.session_platform && (
          <span className="shrink-0 font-mono text-[10px] uppercase tracking-wide text-muted">
            {summary.session_platform}
          </span>
        )}
      </>
    );
  if (!summary.session_openable || !sessionId) {
    return <div className="flex min-w-0 items-center gap-2">{body}</div>;
  }
  return (
    <Link to={`/chat/${sessionId}`} className="flex min-w-0 items-center gap-2 hover:underline">
      {body}
      <ArrowUpRight className="size-3.5 shrink-0 text-cyan" />
    </Link>
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

/** A named group of fields that answer one question, indented under that question.
 *
 * Written for the escalation lane: five fields describing a path that runs on no
 * healthy day, rendered at the same weight as the command and the schedule, said the
 * task's daily work was an Agent turn on Opus. The fields are right (see
 * ``routesSomewhere``) and the fix is not to hide them -- it is to say what they
 * belong to, above them rather than below.
 */
const DetailGroup: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <div className="flex flex-col gap-4 border-l-2 border-border pl-3">
    <div className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{label}</div>
    {children}
  </div>
);

const FailureDetails: React.FC<{
  row: HarnessTask | HarnessWatch;
}> = ({ row }) => {
  const { t } = useTranslation();
  // The mapper uses structured facts only. A stored error without enough
  // structure still proves an unclassified failure, so keep the default copy
  // generic instead of letting raw stderr choose a category.
  const summaryKey =
    definitionFailureSummaryKey(row) ??
    (row.last_error && !definitionHasNeutralWatchExit(row) ? 'harness.failure.generic' : null);
  const disclosureKey = `${'retry_exit_codes' in row ? 'watch' : 'task'}:${row.id}`;
  if (!summaryKey && !row.last_error) return null;
  return (
    <div className="flex flex-col gap-1.5">
      {summaryKey && (
        <>
          <div className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">
            {t('harness.detail.failureSummary')}
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-2 text-[12px] text-pink">
            <span>{t(summaryKey)}</span>
          </div>
        </>
      )}
      {row.last_error && (
        <details
          key={disclosureKey}
          className="group min-w-0 rounded-md border border-border bg-surface-3 px-2 py-1.5"
        >
          <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[10px] font-medium uppercase tracking-wide text-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-cyan/60">
            <ChevronRight aria-hidden className="size-3 shrink-0 transition-transform group-open:rotate-90" />
            {t('harness.detail.technicalDetails')}
          </summary>
          <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-words border-t border-border pt-2 font-mono text-[11px] text-muted">
            {row.last_error}
          </pre>
        </details>
      )}
    </div>
  );
};

const EmptyState: React.FC<{ i18nKey: string }> = ({ i18nKey }) => {
  const { t } = useTranslation();
  return (
    <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center">
      <Activity className="size-6 text-muted" />
      <div className="text-[13px] text-muted">{t(i18nKey)}</div>
    </div>
  );
};
