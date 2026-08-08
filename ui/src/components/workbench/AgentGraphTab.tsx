import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
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
  type AgentGraphNode,
  type AgentGraphResult,
  type AgentGraphTriggerNode,
  type GraphWindow,
  GRAPH_WINDOWS,
  computeFillHeight,
  filterDisabledTriggers,
  isBackground,
  triggerRefId,
} from '../../lib/agentGraph';
import { searchGraph, type GraphSearchResult } from '../../lib/graphSearch';
import { readGraphShowDisabled, writeGraphShowDisabled } from '../../lib/graphViewPrefs';
import { AgentGraphCanvas } from './AgentGraphCanvas';
import { AgentGraphMobileList } from './AgentGraphMobileList';
import { AgentGraphDetail } from './AgentGraphDetail';
import { AgentGraphTriggerDetail } from './AgentGraphTriggerDetail';
import { AgentGraphOrphanStrip } from './AgentGraphOrphanStrip';
import { AgentGraphSearch } from './AgentGraphSearch';

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

// Desktop-only fill height for the canvas + detail panel (design.pen KfgtJ —
// fill_container). The desktop app shell is document-flow (no bounded-height
// ancestor to inherit `h-full` from), so we size the graph area to the viewport
// below its own measured top rather than a fixed calc: wrapped filters / warning
// strips shrink it correctly, and a window resize re-fits it. Floored at
// GRAPH_MIN_HEIGHT so a short window scrolls instead of crushing the canvas. This
// number is a viewport size only — it never feeds the dagre layout — so a resize
// reflows the view without moving a node (mirrors the M3 hover principle).
const GRAPH_FILL_BOTTOM_GAP = 24; // gap between canvas bottom and viewport bottom
const GRAPH_MIN_HEIGHT = 480; // owner floor — below this the page scrolls
const GRAPH_FILL_TOP_ESTIMATE = 280; // first-frame seed only; useLayoutEffect corrects it

function useGraphFillHeight(enabled: boolean): [(el: HTMLDivElement | null) => void, number | undefined] {
  // The grid element mounts only once the graph is loaded, so track it as state:
  // the layout effect re-runs (and measures) exactly when it appears/disappears.
  const [gridEl, setGridEl] = useState<HTMLDivElement | null>(null);
  const gridRef = useCallback((el: HTMLDivElement | null) => setGridEl(el), []);
  const [height, setHeight] = useState<number | undefined>(() =>
    typeof window !== 'undefined'
      ? computeFillHeight(window.innerHeight, GRAPH_FILL_TOP_ESTIMATE, GRAPH_FILL_BOTTOM_GAP, GRAPH_MIN_HEIGHT)
      : undefined,
  );
  useLayoutEffect(() => {
    if (!enabled || !gridEl || typeof window === 'undefined') return;
    let raf = 0;
    const measure = () => {
      const top = gridEl.getBoundingClientRect().top;
      const next = computeFillHeight(window.innerHeight, top, GRAPH_FILL_BOTTOM_GAP, GRAPH_MIN_HEIGHT);
      setHeight((prev) => (prev === next ? prev : next)); // idempotent ⇒ converges
    };
    // rAF-defer so the ResizeObserver never setState's inside its own delivery
    // (avoids the "loop completed with undelivered notifications" warning); the
    // measure is idempotent so it settles to a fixed point in a frame or two.
    const schedule = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(measure);
    };
    measure(); // synchronous first pass, before paint
    window.addEventListener('resize', schedule);
    // Content above the graph (orphan strip, warning banners, a wrapped filter
    // row) shifts our top offset without a window resize — observing the body
    // catches those reflows.
    const ro = new ResizeObserver(schedule);
    ro.observe(document.body);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('resize', schedule);
      ro.disconnect();
    };
  }, [enabled, gridEl]);
  return [gridRef, enabled ? height : undefined];
}

export const AgentGraphTab: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const navigate = useNavigate();
  const isDesktop = useIsDesktop();
  // Desktop canvas + detail panel fill the viewport below their own top; the ref
  // goes on the graph-area grid so the measurement tracks it (mobile ⇒ undefined).
  const [graphAreaRef, fillHeight] = useGraphFillHeight(isDesktop);

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

  // Selection is mutually exclusive: a session node OR a trigger chip, never
  // both (A11 — a chip opens the same right-side panel a node does).
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedTriggerId, setSelectedTriggerId] = useState<string | null>(null);
  const selectNode = useCallback((id: string) => {
    setSelectedNodeId(id);
    setSelectedTriggerId(null);
  }, []);
  const selectTrigger = useCallback((id: string) => {
    setSelectedTriggerId(id);
    setSelectedNodeId(null);
  }, []);

  // A11: disabled trigger chips (+ their trigger edges) are hidden by default;
  // the canvas legend switch reveals them and the choice is remembered locally.
  const [showDisabled, setShowDisabled] = useState(readGraphShowDisabled);
  // A search-hit reveal of a hidden disabled chip is TRANSIENT — it must not
  // write the persisted preference (owner ruling): only an explicit legend /
  // mobile toggle persists. This session-only flag OR's into the effective
  // visibility so a searched-for chip renders without changing what the user
  // sees on the next load.
  const [revealDisabled, setRevealDisabled] = useState(false);
  const effectiveShowDisabled = showDisabled || revealDisabled;
  const setShowDisabledPersisted = useCallback((next: boolean) => {
    setShowDisabled(next);
    writeGraphShowDisabled(next);
    // An explicit hide also clears any transient reveal, so "off" always hides.
    if (!next) setRevealDisabled(false);
  }, []);

  // ── Node search (M8) ──────────────────────────────────────────────────────
  // The display graph is server-filtered, so search runs over a separate broad
  // "index" payload (this window/project, but always incl. ended + background)
  // fetched lazily on first focus and cached by window/project. Hits outside the
  // visible filters get badged; selecting one widens the filters, then locates.
  const [searchQuery, setSearchQuery] = useState('');
  const [searchIndex, setSearchIndex] = useState<{
    key: string;
    nodes: AgentGraphNode[];
    triggers: AgentGraphTriggerNode[];
  } | null>(null);
  const [searchIndexLoading, setSearchIndexLoading] = useState(false);
  const searchSeqRef = useRef(0);
  // Set on any display refresh (filter change / SSE / poll) so the next focus
  // refetches a fresh index; the stale copy stays usable until then.
  const searchStaleRef = useRef(false);
  // Locate request handed to the canvas (nonce bumps per request).
  const [locate, setLocate] = useState<{ rfId: string; kind: 'node' | 'trigger'; nonce: number } | null>(null);
  const locateNonceRef = useRef(0);
  // A locate that must wait for a filter-widening refetch to surface its target.
  // `graphAtRequest` pins the payload present when the user selected, so the
  // resolver only decides once a genuinely newer payload lands (superseded
  // in-flight fetches never call setGraph, so graph identity is the signal).
  const [pendingLocate, setPendingLocate] = useState<{
    kind: 'node' | 'trigger';
    id: string;
    rfId: string;
    graphAtRequest: GraphPayload | null;
  } | null>(null);

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
        // The graph just changed (filter/SSE/poll); the search index may now be
        // out of date. Mark it stale so the next search-box focus refetches —
        // the current copy stays usable so an open dropdown doesn't blank out.
        searchStaleRef.current = true;
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
  // Raw (unfiltered) trigger map — the detail panels read lineage from this so a
  // node's trigger still shows even when the chip is hidden by the legend toggle.
  const triggersById = useMemo(
    () => new Map(triggerNodes.map((tr) => [tr.definition_id, tr])),
    [triggerNodes],
  );

  // A11 display filter: drop disabled chips + their trigger edges (unless the
  // legend switch is on OR a search reveal is active) before they reach the
  // canvas/mobile list. Returned by reference when nothing is filtered, so the
  // layout stays stable otherwise.
  const { triggerNodes: displayTriggers, edges: displayEdges } = useMemo(
    () => filterDisabledTriggers(triggerNodes, edges, effectiveShowDisabled),
    [triggerNodes, edges, effectiveShowDisabled],
  );
  // The set of chips actually rendered — search membership ("outside filters")
  // and reveal-on-select are judged against this, not the raw trigger map.
  const displayTriggerIds = useMemo(
    () => new Set(displayTriggers.map((tr) => tr.definition_id)),
    [displayTriggers],
  );

  // Drop a stale selection once its node/chip leaves the payload. A selected chip
  // survives a legend toggle (it's still in the raw payload) but not aging out.
  useEffect(() => {
    if (selectedNodeId && !nodesById.has(selectedNodeId)) setSelectedNodeId(null);
  }, [selectedNodeId, nodesById]);
  useEffect(() => {
    if (selectedTriggerId && !triggersById.has(selectedTriggerId)) setSelectedTriggerId(null);
  }, [selectedTriggerId, triggersById]);

  const selectedNode = selectedNodeId ? nodesById.get(selectedNodeId) ?? null : null;
  const selectedTrigger = selectedTriggerId ? triggersById.get(selectedTriggerId) ?? null : null;
  const detailOpen = !!selectedNode || !!selectedTrigger;

  // The detail panel matches the canvas height on desktop (design.pen KfgtJ) so
  // the two columns read as one pane; its body scrolls when the content is taller
  // than the fill. On mobile (fillHeight === undefined) it stays natural-height.
  const panelBoxClass = clsx(
    'rounded-2xl border border-border-strong bg-surface p-5',
    fillHeight != null ? 'overflow-y-auto' : 'self-start',
  );
  const panelBoxStyle = fillHeight != null ? { height: fillHeight } : undefined;

  // Lazy-load the broad search index for the current window/project. Skipped
  // when a fresh copy for this key is already cached; a stale flag (set by any
  // display refresh) forces a refetch on the next focus.
  const ensureSearchIndex = useCallback(async () => {
    const key = `${windowSel}|${projectSel}`;
    if (searchIndex?.key === key && !searchStaleRef.current) return;
    const seq = ++searchSeqRef.current;
    setSearchIndexLoading(true);
    try {
      const res = await api.getAgentsGraph({
        window: windowSel,
        project: projectSel,
        includeEnded: true,
        includeBackground: true,
      });
      if (!mountedRef.current || seq !== searchSeqRef.current) return;
      searchStaleRef.current = false;
      setSearchIndex({ key, nodes: res.nodes, triggers: res.trigger_nodes });
    } catch {
      // Leave any prior index in place; the box just shows no fresh results.
    } finally {
      if (mountedRef.current && seq === searchSeqRef.current) setSearchIndexLoading(false);
    }
  }, [api, windowSel, projectSel, searchIndex]);

  const searchResults = useMemo<GraphSearchResult[]>(() => {
    // Gate on the current window/project key: after a filter change the cached
    // index is for the old key until the focus-triggered refetch lands, and its
    // hits can't be revealed (the select path only widens active/background, not
    // project/window). Show nothing until the fresh index arrives.
    const key = `${windowSel}|${projectSel}`;
    if (!searchIndex || searchIndex.key !== key) return [];
    // Both node and trigger hits are actionable on desktop AND mobile now: a
    // trigger opens the same right-side detail panel (A11), so no kind is dropped.
    return searchGraph(searchQuery, searchIndex.nodes, searchIndex.triggers);
  }, [searchQuery, searchIndex, windowSel, projectSel]);

  // A hit is "outside current filters" when it isn't currently rendered. For a
  // trigger that means either aged out of the window or hidden by the disabled
  // toggle — both judged against the rendered chip set, so a disabled chip badges
  // "outside filters" while the toggle is off and reveals itself on select.
  const isOutsideFilters = useCallback(
    (r: GraphSearchResult) => (r.kind === 'node' ? !nodesById.has(r.id) : !displayTriggerIds.has(r.id)),
    [nodesById, displayTriggerIds],
  );

  const requestLocate = useCallback((kind: 'node' | 'trigger', rfId: string) => {
    setLocate({ rfId, kind, nonce: ++locateNonceRef.current });
  }, []);

  // Select a search result: if it's already visible, select + locate now; if it
  // sits outside the filters, widen the needed toggle(s) and defer the locate to
  // the pendingLocate effect once the refetch surfaces it.
  const onSelectResult = useCallback(
    (r: GraphSearchResult) => {
      const rfId = r.kind === 'node' ? r.id : triggerRefId(r.id);
      if (r.kind === 'node') {
        // Already rendered → select + locate now.
        if (nodesById.has(r.id)) {
          selectNode(r.id);
          requestLocate('node', rfId);
          return;
        }
      } else {
        // A rendered chip → select + locate now.
        if (displayTriggerIds.has(r.id)) {
          selectTrigger(r.id);
          requestLocate('trigger', rfId);
          return;
        }
        // In the payload but hidden by the "show disabled" toggle: M8's
        // reveal-on-click — reveal TRANSIENTLY (session-only, no persisted pref),
        // select, and locate. A pure client filter flip (no refetch); the canvas
        // re-renders with the chip and the nonce'd locate resolves once it's out.
        if (triggersById.has(r.id)) {
          setRevealDisabled(true);
          selectTrigger(r.id);
          requestLocate('trigger', rfId);
          return;
        }
      }
      // Outside the payload entirely: widen the server filters (index & display
      // share window/project, so only active/background differ) and defer the
      // locate to the pendingLocate effect once the refetch surfaces it.
      let flipped = false;
      if (r.kind === 'node') {
        if (isBackground(r.node) && !showBackground) {
          setShowBackground(true);
          flipped = true;
        }
        // Active view keeps only live + queued; an ended/idle node needs 含历史.
        if (mode === 'active' && !(r.node.live || r.node.status === 'queued')) {
          setMode('history');
          flipped = true;
        }
      } else {
        // A trigger chip only draws when its triggered session is in-window;
        // widen both so the session (and thus the chip) can reappear.
        if (!showBackground) {
          setShowBackground(true);
          flipped = true;
        }
        if (mode === 'active') {
          setMode('history');
          flipped = true;
        }
        // A disabled chip stays filtered out even after it returns unless
        // disabled chips are shown — reveal it TRANSIENTLY (session-only, no
        // persisted pref). This is a client-only filter (never triggers a
        // refetch), so it must NOT count toward `flipped`.
        if (!r.trigger.enabled && !effectiveShowDisabled) {
          setRevealDisabled(true);
        }
      }
      setPendingLocate({ kind: r.kind, id: r.id, rfId, graphAtRequest: graph });
      // No server filter changed ⇒ no automatic refetch is coming; force one so
      // the pendingLocate effect either surfaces the target or (graph identity
      // changed, still absent) toasts rather than hanging.
      if (!flipped) void fetchGraph(false);
    },
    [
      nodesById,
      triggersById,
      displayTriggerIds,
      showBackground,
      mode,
      effectiveShowDisabled,
      selectNode,
      selectTrigger,
      setRevealDisabled,
      requestLocate,
      fetchGraph,
      graph,
    ],
  );

  // Resolve a deferred locate: fire once the widened payload includes the
  // target; once a genuinely newer payload has landed (graph identity changed)
  // and it's still missing, it left the window between index load and select —
  // tell the user, don't hang.
  useEffect(() => {
    if (!pendingLocate) return;
    // A trigger must be in the *rendered* set (not just the raw payload) to be
    // located — the reveal flip in onSelectResult guarantees a disabled chip is
    // rendered by the time its refetch lands.
    const present =
      pendingLocate.kind === 'node'
        ? nodesById.has(pendingLocate.id)
        : displayTriggerIds.has(pendingLocate.id);
    if (present) {
      if (pendingLocate.kind === 'node') selectNode(pendingLocate.id);
      else selectTrigger(pendingLocate.id);
      requestLocate(pendingLocate.kind, pendingLocate.rfId);
      setPendingLocate(null);
    } else if (graph !== pendingLocate.graphAtRequest) {
      showToast(t('agents.graph.search.noLongerInWindow'), 'warning');
      setPendingLocate(null);
    }
  }, [pendingLocate, nodesById, displayTriggerIds, graph, selectNode, selectTrigger, requestLocate, showToast, t]);

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
        <AgentGraphSearch
          query={searchQuery}
          onQueryChange={setSearchQuery}
          results={searchResults}
          onFocus={() => void ensureSearchIndex()}
          onSelect={onSelectResult}
          loading={searchIndexLoading}
          isOutsideFilters={isOutsideFilters}
        />
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
        {/* Mobile has no canvas legend, so surface the disabled-trigger toggle here;
            otherwise a transient search-reveal can never be turned off on mobile. This
            switch persists the preference; its checked state tracks the effective value
            so a transient reveal shows as on and can be toggled back off. */}
        {!isDesktop && (
          <label className="inline-flex items-center gap-2 text-[12px] text-muted">
            {t('agents.graph.legend.showDisabled')}
            <Switch checked={effectiveShowDisabled} onCheckedChange={setShowDisabledPersisted} label={t('agents.graph.legend.showDisabled')} />
          </label>
        )}
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
          ref={graphAreaRef}
          className={clsx(
            'grid gap-4',
            detailOpen ? 'grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px]' : 'grid-cols-1',
          )}
        >
          <div className={clsx('min-w-0', detailOpen && 'max-lg:hidden')}>
            {nodes.length === 0 ? (
              <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center text-[13px] text-muted">
                {t('agents.graph.empty')}
              </div>
            ) : isDesktop ? (
              <AgentGraphCanvas
                nodes={nodes}
                triggerNodes={displayTriggers}
                edges={displayEdges}
                selectedId={selectedNodeId}
                selectedTriggerId={selectedTriggerId}
                showDisabled={effectiveShowDisabled}
                onToggleDisabled={setShowDisabledPersisted}
                heightPx={fillHeight}
                // Refit the viewport when the filters change the layout (small→
                // large graph, different project/window, or revealing disabled
                // chips); SSE-only refreshes keep the same key and preserve the
                // current pan/zoom. A search-locate that reveals disabled chips
                // still wins — it re-pins fittedRef and its setCenter runs after
                // this refit in the same frame.
                fitKey={`${windowSel}|${projectSel}|${mode}|${showBackground}|${effectiveShowDisabled}`}
                locate={locate}
                onSelectNode={selectNode}
                onSelectTrigger={selectTrigger}
                onOpenChat={(id) => navigate(`/chat/${encodeURIComponent(id)}`)}
              />
            ) : (
              <AgentGraphMobileList
                nodes={nodes}
                edges={displayEdges}
                triggerNodes={displayTriggers}
                selectedId={selectedNodeId}
                onSelectNode={selectNode}
                onSelectTrigger={selectTrigger}
              />
            )}
          </div>

          {/* One right-side panel, mutually exclusive: a session node or a
              trigger chip (A11). Both read lineage from the raw edges/maps so a
              hidden-by-toggle relation still shows truthfully. */}
          {selectedNode ? (
            <div className={panelBoxClass} style={panelBoxStyle}>
              <AgentGraphDetail
                node={selectedNode}
                nodesById={nodesById}
                edges={edges}
                triggersById={triggersById}
                onClose={() => setSelectedNodeId(null)}
                onSelectNode={selectNode}
                onRefresh={() => fetchGraph(true)}
              />
            </div>
          ) : selectedTrigger ? (
            <div className={panelBoxClass} style={panelBoxStyle}>
              <AgentGraphTriggerDetail
                trigger={selectedTrigger}
                edges={edges}
                nodesById={nodesById}
                onClose={() => setSelectedTriggerId(null)}
                onSelectNode={selectNode}
                onRefresh={() => fetchGraph(true)}
              />
            </div>
          ) : null}
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
