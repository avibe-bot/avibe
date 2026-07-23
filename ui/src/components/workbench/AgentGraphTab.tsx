import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { AlertTriangle, ChevronDown, Clock, FolderClosed, Loader2, RefreshCw, ServerCrash } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import type { RunningAgent } from '../../context/ApiContext';
import { useToast } from '../../context/ToastContext';
import { Button } from '../ui/button';
import { Switch } from '../ui/switch';
import { SegmentedRadio } from '../ui/segmented';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import {
  type AgentGraphResult,
  type GraphWindow,
  GRAPH_WINDOWS,
} from '../../lib/agentGraph';
import { AgentGraphCanvas } from './AgentGraphCanvas';
import { AgentGraphMobileList } from './AgentGraphMobileList';
import { AgentGraphDetail } from './AgentGraphDetail';
import { AgentGraphOrphanStrip } from './AgentGraphOrphanStrip';

// Degraded-mode refresh cadence while SSE is disconnected (mirrors the old
// RunningAgentsTab). SSE covers lifecycle writes when connected.
const POLL_INTERVAL_MS = 4000;
const LIVENESS_POLL_INTERVAL_MS = 30000;

type GraphPayload = AgentGraphResult & { live_unreachable?: boolean };

// Desktop ⇒ React Flow canvas; mobile ⇒ grouped list (contract §4).
function useIsDesktop(): boolean {
  const [desktop, setDesktop] = useState(
    () => typeof window !== 'undefined' && window.matchMedia?.('(min-width: 768px)').matches,
  );
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mql = window.matchMedia('(min-width: 768px)');
    const onChange = () => setDesktop(mql.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, []);
  return !!desktop;
}

export const AgentGraphTab: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const navigate = useNavigate();
  const isDesktop = useIsDesktop();

  const [graph, setGraph] = useState<GraphPayload | null>(null);
  // Session-less orphan processes (contract A3) — surfaced in a strip above the
  // graph, not as nodes. Sourced from the running-agents snapshot, so they are
  // filter-independent like the badge.
  const [orphans, setOrphans] = useState<RunningAgent[]>([]);
  const [loading, setLoading] = useState(true);
  const [errored, setErrored] = useState(false);
  const [eventBridgeConnected, setEventBridgeConnected] = useState(false);
  const [projects, setProjects] = useState<{ id: string; display_name: string }[]>([]);

  // Filters (spec: 活跃/含历史 · time window · project incl. 独立 · 显示后台会话).
  const [mode, setMode] = useState<'active' | 'history'>('history');
  const [windowSel, setWindowSel] = useState<GraphWindow>('24h');
  const [projectSel, setProjectSel] = useState<string>('all');
  const [showBackground, setShowBackground] = useState(true);

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Project dropdown options come from the authoritative project list so the
  // menu stays complete even when a filter narrows the graph.
  useEffect(() => {
    let cancelled = false;
    api
      .listProjects()
      .then((res) => {
        if (!cancelled) setProjects(res.projects.map((p) => ({ id: p.id, display_name: p.display_name })));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [api]);

  const seqRef = useRef(0);
  const inFlightRef = useRef(false);
  const pendingBgRef = useRef(false);
  const fetchGraph = useCallback(
    async (background = false) => {
      // Coalesce background refreshes: the poll and the SSE bus (which can burst
      // many run/turn events) both call this. While a fetch is in flight, record
      // that another was requested and run exactly one trailing refresh when it
      // settles — `seqRef` only discards stale *results*, it doesn't stop
      // concurrent scans from piling up on a slow/backgrounded tab.
      if (background && inFlightRef.current) {
        pendingBgRef.current = true;
        return;
      }
      const seq = ++seqRef.current;
      inFlightRef.current = true;
      if (!background) setLoading(true);
      try {
        // Orphans come from the (filter-independent) running-agents snapshot in
        // parallel with the filtered graph; a running-agents failure must not
        // fail the graph fetch.
        const [result, running] = await Promise.all([
          api.getAgentsGraph({
            window: windowSel,
            project: projectSel,
            includeEnded: mode === 'history',
            includeBackground: showBackground,
          }),
          api.getRunningAgents().catch(() => null),
        ]);
        // Ignore a stale response: a slower earlier request must not clobber a
        // newer one issued after a filter change.
        if (!mountedRef.current || seq !== seqRef.current) return;
        setGraph(result);
        // Every session-less live row (any state) goes to the strip — the graph
        // is session-centric so these have no node, and the old flat list let
        // users end them. The strip labels each by its actual state and offers a
        // state-appropriate action (Stop/Disconnect/Kill).
        setOrphans(running && running.ok ? running.agents.filter((a) => !a.session_id) : []);
        setErrored(false);
      } catch {
        if (mountedRef.current && seq === seqRef.current) setErrored(true);
      } finally {
        inFlightRef.current = false;
        // Always clear the spinner a foreground fetch raised — even if a newer
        // fetch superseded its result — or a superseded foreground load leaves
        // loading stuck true forever.
        if (mountedRef.current && !background) setLoading(false);
        // Run the single coalesced refresh requested while this one was in
        // flight, so bursts collapse to one trailing fetch — but only if no
        // newer fetch has started. If the user changed a filter (or any newer
        // fetch bumped seqRef) the current `fetchGraph` closure holds stale
        // params; replaying it would overwrite the graph with the old filter's
        // results. The newer in-flight fetch already covers freshness, so drop
        // the stale trailing refresh.
        if (mountedRef.current && pendingBgRef.current && seq === seqRef.current) {
          pendingBgRef.current = false;
          void fetchGraph(true);
        } else if (seq !== seqRef.current) {
          pendingBgRef.current = false;
        }
      }
    },
    [api, windowSel, projectSel, mode, showBackground],
  );

  // Kill a session-less orphan process (A3): orphan teardown resolves by pid, so
  // pass the snapshot row's identifiers straight through.
  const killOrphan = useCallback(
    async (orphan: RunningAgent) => {
      try {
        const result = await api.endRunningAgent({
          backend: orphan.backend,
          state: orphan.state,
          session_id: orphan.session_id,
          composite_key: orphan.composite_key,
          base_session_id: orphan.base_session_id,
          pid: orphan.pid,
        });
        if (result.ok) {
          showToast(t('agents.running.endedToast'), 'success');
          void fetchGraph(true);
        } else {
          showToast(t('agents.running.endFailedToast', { error: result.error || 'failed' }), 'error');
        }
      } catch (err) {
        showToast(err instanceof Error ? err.message : String(err), 'error');
      }
    },
    [api, showToast, t, fetchGraph],
  );

  useEffect(() => {
    void fetchGraph(false);
  }, [fetchGraph]);

  // Realtime: reuse the workbench SSE bus (runs/turn/session events) — no new
  // transport (contract). Refetch in the background on any signal.
  useEffect(() => {
    return api.connectWorkbenchEvents({
      onConnected: (data) => {
        if (data.source === 'controller') {
          setEventBridgeConnected(true);
          void fetchGraph(true);
        }
      },
      onEventBridgeStatus: ({ connected }) => {
        setEventBridgeConnected(connected);
        if (connected) void fetchGraph(true);
      },
      onError: () => setEventBridgeConnected(false),
      onRunsUpdated: () => void fetchGraph(true),
      onTurnStart: () => void fetchGraph(true),
      onTurnEnd: () => void fetchGraph(true),
      onSessionStatus: () => void fetchGraph(true),
      // Visibility/scope/project moves arrive as session.activity — refetch so a
      // session leaves/enters its bucket immediately instead of waiting for the
      // 30s liveness poll.
      onSessionActivity: () => void fetchGraph(true),
    });
  }, [api, fetchGraph]);

  // Low-rate reconciliation poll (orphan/liveness is a sampled snapshot).
  useEffect(() => {
    const intervalMs = eventBridgeConnected ? LIVENESS_POLL_INTERVAL_MS : POLL_INTERVAL_MS;
    let timer: number | undefined;
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      if (document.visibilityState === 'visible') void fetchGraph(true);
      timer = window.setTimeout(tick, intervalMs);
    };
    timer = window.setTimeout(tick, intervalMs);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [eventBridgeConnected, fetchGraph]);

  // Memoize so the empty-fallback arrays keep a stable identity across renders
  // (they feed downstream useMemo dependency lists).
  const nodes = useMemo(() => graph?.nodes ?? [], [graph]);
  const triggerNodes = useMemo(() => graph?.trigger_nodes ?? [], [graph]);
  const edges = useMemo(() => graph?.edges ?? [], [graph]);

  const nodesById = useMemo(() => new Map(nodes.map((n) => [n.session_id, n])), [nodes]);
  const triggersById = useMemo(
    () => new Map(triggerNodes.map((tr) => [tr.definition_id, tr])),
    [triggerNodes],
  );

  // Drop a stale selection once its node leaves the payload.
  useEffect(() => {
    if (selectedNodeId && !nodesById.has(selectedNodeId)) setSelectedNodeId(null);
  }, [selectedNodeId, nodesById]);

  const selectedNode = selectedNodeId ? nodesById.get(selectedNodeId) ?? null : null;

  const onSelectTrigger = useCallback(
    (definitionId: string) => {
      const tab = triggersById.get(definitionId)?.definition_type === 'watch' ? 'watches' : 'tasks';
      // Land on the matching Harness definitions tab (deep-select by id is a
      // follow-up once Harness supports a ?task/?watch URL anchor).
      navigate(`/harness?tab=${tab}`);
    },
    [navigate, triggersById],
  );

  const projectLabel = useMemo(() => {
    if (projectSel === 'all') return t('agents.graph.filters.projectAll');
    if (projectSel === 'standalone') return t('agents.graph.detail.standalone');
    return projects.find((p) => p.id === projectSel)?.display_name ?? projectSel;
  }, [projectSel, projects, t]);

  const counts = graph?.counts;

  return (
    <div className="flex flex-col gap-4">
      {/* Header strip: subtitle + live pill */}
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-0 flex-1 text-[12.5px] text-muted">{t('agents.graph.subtitle')}</p>
        {counts && (
          <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-mint/40 bg-mint-soft px-3 py-1 text-[12px] font-semibold text-mint">
            <span className="size-1.5 rounded-full bg-mint" />
            {t('agents.graph.livePill', { active: counts.active, queued: counts.queued })}
          </span>
        )}
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-2.5">
        <SegmentedRadio
          value={mode}
          onChange={setMode}
          ariaLabel={t('agents.graph.filters.modeLabel')}
          options={[
            { id: 'active', label: t('agents.graph.filters.active') },
            { id: 'history', label: t('agents.graph.filters.withHistory') },
          ]}
        />
        <FilterDropdown
          icon={<Clock className="size-3 text-muted" />}
          label={t(`agents.graph.window.${windowSel}`)}
        >
          {(close) =>
            GRAPH_WINDOWS.map((w) => (
              <DropdownItem
                key={w}
                active={w === windowSel}
                onClick={() => {
                  setWindowSel(w);
                  close();
                }}
              >
                {t(`agents.graph.window.${w}`)}
              </DropdownItem>
            ))
          }
        </FilterDropdown>
        <FilterDropdown
          icon={<FolderClosed className="size-3 text-muted" />}
          label={`${t('agents.graph.filters.project')}: ${projectLabel}`}
        >
          {(close) => (
            <>
              {[
                { value: 'all', label: t('agents.graph.filters.projectAll') },
                { value: 'standalone', label: t('agents.graph.detail.standalone') },
                ...projects.map((p) => ({ value: p.id, label: p.display_name })),
              ].map((opt) => (
                <DropdownItem
                  key={opt.value}
                  active={opt.value === projectSel}
                  onClick={() => {
                    setProjectSel(opt.value);
                    close();
                  }}
                >
                  {opt.label}
                </DropdownItem>
              ))}
            </>
          )}
        </FilterDropdown>
        <span className="flex-1" />
        <label className="inline-flex items-center gap-2 text-[12px] text-muted">
          {t('agents.graph.filters.showBackground')}
          <Switch checked={showBackground} onCheckedChange={setShowBackground} label={t('agents.graph.filters.showBackground')} />
        </label>
        <Button type="button" variant="outline" size="xs" onClick={() => fetchGraph(false)} disabled={loading}>
          <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} />
          {t('common.refresh')}
        </Button>
      </div>

      {graph?.live_unreachable && (
        <div className="flex items-center gap-2 rounded-lg border border-gold/40 bg-gold/[0.06] px-3 py-2 text-[12px] text-gold">
          <ServerCrash className="size-3.5" />
          {t('agents.graph.unreachable')}
        </div>
      )}

      {graph?.truncated && (
        <div className="flex items-center gap-2 rounded-lg border border-gold/40 bg-gold/[0.06] px-3 py-2 text-[12px] text-gold">
          <AlertTriangle className="size-3.5 shrink-0" />
          {t('agents.graph.truncated')}
        </div>
      )}

      {/* Session-less live processes strip (A3 + r6) — above the graph. */}
      <AgentGraphOrphanStrip rows={orphans} onEnd={killOrphan} />

      {loading && !graph ? (
        <div className="flex items-center justify-center py-16">
          <Loader2 className="size-5 animate-spin text-muted" />
        </div>
      ) : errored && !graph ? (
        <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-amber-500/40 bg-amber-500/[0.04] px-6 py-12 text-center">
          <ServerCrash className="size-8 text-amber-500" />
          <div className="text-[13px] text-muted">{t('agents.graph.error')}</div>
          <Button type="button" variant="outline" size="xs" onClick={() => fetchGraph(false)}>
            <RefreshCw className="size-3.5" />
            {t('common.refresh')}
          </Button>
        </div>
      ) : (
        <div
          className={clsx(
            'grid gap-4',
            selectedNode ? 'grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]' : 'grid-cols-1',
          )}
        >
          <div className={clsx('min-w-0', selectedNode && 'max-lg:hidden')}>
            {nodes.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center text-[13px] text-muted">
                {t('agents.graph.empty')}
              </div>
            ) : isDesktop ? (
              <AgentGraphCanvas
                nodes={nodes}
                triggerNodes={triggerNodes}
                edges={edges}
                selectedId={selectedNodeId}
                // Refit the viewport when the filters change the layout (small→
                // large graph, different project/window); SSE-only refreshes keep
                // the same key and preserve the current pan/zoom.
                fitKey={`${windowSel}|${projectSel}|${mode}|${showBackground}`}
                onSelectNode={setSelectedNodeId}
                onSelectTrigger={onSelectTrigger}
                onOpenChat={(id) => navigate(`/chat/${encodeURIComponent(id)}`)}
              />
            ) : (
              <AgentGraphMobileList
                nodes={nodes}
                edges={edges}
                triggerNodes={triggerNodes}
                selectedId={selectedNodeId}
                onSelectNode={setSelectedNodeId}
              />
            )}
          </div>

          {selectedNode && (
            <div className="self-start rounded-2xl border border-border-strong bg-surface p-5">
              <AgentGraphDetail
                node={selectedNode}
                nodesById={nodesById}
                edges={edges}
                triggersById={triggersById}
                onClose={() => setSelectedNodeId(null)}
                onSelectNode={setSelectedNodeId}
                onRefresh={() => fetchGraph(true)}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
};

// ── compact filter dropdown (Popover) — mirrors AgentsPage BackendFilter ──────

interface FilterDropdownProps {
  icon: React.ReactNode;
  label: string;
  children: (close: () => void) => React.ReactNode;
}

const FilterDropdown: React.FC<FilterDropdownProps> = ({ icon, label, children }) => {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-1.5 rounded-md border border-border-strong bg-surface px-3 py-2 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
        >
          {icon}
          <span className="max-w-[160px] truncate">{label}</span>
          <ChevronDown className="size-3 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="max-h-[280px] w-[200px] overflow-y-auto p-1">
        {children(() => setOpen(false))}
      </PopoverContent>
    </Popover>
  );
};

const DropdownItem: React.FC<{ active: boolean; onClick: () => void; children: React.ReactNode }> = ({
  active,
  onClick,
  children,
}) => (
  <button
    type="button"
    onClick={onClick}
    className={clsx(
      'flex w-full items-center gap-2 truncate rounded px-2 py-1.5 text-left text-[12px] transition',
      active ? 'bg-cyan-soft text-cyan' : 'text-foreground hover:bg-foreground/[0.04]',
    )}
  >
    {children}
  </button>
);
