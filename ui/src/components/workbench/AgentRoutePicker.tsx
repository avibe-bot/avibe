import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { Check, ChevronDown, ChevronLeft, ChevronRight, Loader2, Plus, Search, Sparkles } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import type { SessionSpeed, VibeAgentBrief } from '../../context/ApiContext';
import { useInstanceAuthorization } from '../../context/InstanceAuthorizationContext';
import type { EffortMenu } from '../settings/models/types';
import { loadBackendModelsWithRefresh, modelOptionLabel, supersedesHubAnswer, useAddModelExit } from '../../lib/backendModels';
import { isEffortSupported, refusedSavedEffort, resolveEffortOptions } from '../../lib/effortOptions';
import { useSourceNames } from '../../lib/useSourceNames';
import {
  rememberFastModels,
  rememberModelLabels,
  useRememberedFastModel,
  useRememberedModelLabel,
} from '../../lib/modelLabelMemory';
import { useRouteSurfaceActive } from '../../lib/routeSurfaceActivity';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { ResponsiveMenu } from '../ui/responsive-menu';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { BackendAvatar } from '../visual';
import { getBackendUiMeta } from '@/lib/agentBackends';

// The route fields the picker reads. A subset of WorkbenchSession (chat) and of
// WorkbenchSessionCreate (the create flow), so both can pass their object directly.
export interface AgentRouteValue {
  agent_backend?: string | null;
  agent_name?: string | null;
  agent_id?: string | null;
  agent_variant?: string | null;
  model?: string | null;
  reasoning_effort?: string | null;
}

// The patch onChange emits. agent_variant tracks the backend so a session can
// resume its native thread (the native-session map is keyed by agent_variant).
export interface AgentRoutePatch extends AgentRouteValue {
  agent_variant?: string | null;
}

interface AgentRoutePickerProps {
  value: AgentRouteValue;
  agents: VibeAgentBrief[];
  /** Applied by the OWNER, synchronously. An owner whose write is async applies
   *  the pick to its own state first and persists behind it (see `saving`) —
   *  the picker holds no selection of its own, so anything it waits for is a
   *  highlight the user watches lag behind their click. */
  onChange: (patch: AgentRoutePatch) => void;
  /** Optional backend allow-list for existing sessions whose backend is pinned. */
  allowedBackends?: string[];
  /** Trigger label when no agent is selected — the create flow shows "默认 · …". */
  defaultLabel?: string;
  /** Concrete route to display while `value` is inheriting a project/global default. */
  defaultRoute?: AgentRouteValue;
  /** True when the displayed route is inherited, not persisted on this session/project yet. */
  isDefaultRoute?: boolean;
  /** An owner-side write is in flight — shown as a spinner on the trigger. The
   *  menu stays live: the local state already moved, so further picks queue
   *  behind it instead of being locked out until the request lands. */
  saving?: boolean;
  disabled?: boolean;
  align?: 'start' | 'end';
  /** Override the trigger width (chat caps at 62%; the create surfaces go full width). */
  triggerClassName?: string;
  /** ``columns`` (default): the bordered trigger and the three-column cascade.
   *  ``compact``: a borderless trigger and a model-first menu for the chat
   *  composer — models and efforts up front, the Agent one level down — shown as
   *  a popover on desktop and a bottom sheet on phones. */
  presentation?: 'columns' | 'compact';
  /** Make the popover a modal layer — required when nested in a modal Dialog (the
   *  new-session sheet) so its content isn't aria-hidden/inert by the dialog. */
  modal?: boolean;
  /** Called before navigating to /agents — the create sheet uses it to close
   *  itself so the destination isn't left behind a focus-trapped modal. The
   *  Model Hub exit does not call it: Settings opens over this surface, and a
   *  host suspends under it with its draft intact, as it does for any visit. */
  onNavigateAway?: () => void;
  /** The route's processing tier (compact only, docs/plans/fast-mode.md). With
   *  ``onSpeedChange`` the menu offers the Fast toggle beside the effort, and the
   *  closed trigger marks a route that runs fast. Applied by the owner, like
   *  ``onChange``. */
  speed?: SessionSpeed;
  onSpeedChange?: (speed: SessionSpeed) => void;
}

type ReasoningOption = { value: string; label: string };

const EMPTY_REASONING_OPTIONS: Record<string, ReasoningOption[]> = {};

// design.pen Q5xIZa — one cyan-ringed trigger showing `[backend] agent · model ·
// effort` that opens a three-column cascading menu (Agent → Model → Effort).
// Picking an agent seeds model/effort from its defaults; the model column is
// fetched lazily per backend. CONTROLLED so it serves both the chat header
// (value=session, onChange patches the session) and the create surfaces
// (value=draft route, onChange updates the draft) — one source of truth. On
// phones the three columns stack into one scrollable list.
export const AgentRoutePicker: React.FC<AgentRoutePickerProps> = ({
  value,
  agents,
  onChange,
  allowedBackends,
  defaultLabel,
  defaultRoute,
  isDefaultRoute,
  saving,
  disabled,
  align = 'end',
  triggerClassName,
  presentation = 'columns',
  modal,
  onNavigateAway,
  speed = null,
  onSpeedChange,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const authorization = useInstanceAuthorization();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const compact = presentation === 'compact';
  // The compact menu's Agent list is a second page, not a column.
  const [agentPage, setAgentPage] = useState(false);
  const [modelsByBackend, setModelsByBackend] = useState<Record<string, string[]>>({});
  const [modelLabelsByBackend, setModelLabelsByBackend] = useState<Record<string, Record<string, string>>>({});
  const [reasoningByBackend, setReasoningByBackend] = useState<
    Record<string, Record<string, ReasoningOption[]>>
  >({});
  // Per backend, per model: what routing would do with its efforts now (Model
  // Hub's `effort_menu`), for the column's "not on: <Source>" hints and a saved
  // effort it refuses everywhere.
  const [effortMenusByBackend, setEffortMenusByBackend] = useState<
    Record<string, Record<string, EffortMenu>>
  >({});
  const loadedModelBackendsRef = useRef<Set<string>>(new Set());
  // Backends whose catalog Model Hub answered: their effort menus are read on
  // every open, even after one open fell back to the native list.
  const hubBackendsRef = useRef<Set<string>>(new Set());
  const surfaceActive = useRouteSurfaceActive();
  const [loadingModels, setLoadingModels] = useState(false);
  // Free-text filter for the model column — long backends (OpenCode) list dozens.
  const [modelQuery, setModelQuery] = useState('');

  const hasExplicitRoute = Boolean(value.agent_name || value.agent_backend);
  const routeIsDefault = isDefaultRoute ?? !hasExplicitRoute;
  const displayValue = routeIsDefault ? (defaultRoute ?? value) : value;
  const displayedAgent = displayValue.agent_name
    ? agents.find((agent) => agent.name === displayValue.agent_name) || null
    : null;

  // Emit the pick straight to the owner: an owner with an async write applies it
  // locally and queues the request, so ordering is settled where the state lives
  // (an Agent pick carries that Agent's default model/effort, and an earlier
  // request landing last would undo a newer model pick). Blocking here would
  // only delay the highlight — and lock out the very next click.
  const applyPatch = useCallback(
    (changes: AgentRoutePatch) => {
      // Picking a model/effort on an INHERITED route must persist the whole
      // displayed route, not just the changed field: otherwise the owner stores
      // a bare model against no Agent at all.
      const patchPinsDisplayedDefault =
        routeIsDefault &&
        Boolean(displayValue.agent_name || displayValue.agent_backend) &&
        !('agent_name' in changes) &&
        !('agent_backend' in changes) &&
        !('agent_id' in changes) &&
        !('agent_variant' in changes);
      onChange(
        patchPinsDisplayedDefault
          ? {
              agent_name: displayValue.agent_name ?? null,
              agent_id: displayValue.agent_id ?? null,
              agent_backend: displayedAgent?.backend ?? displayValue.agent_backend ?? null,
              agent_variant: displayValue.agent_variant ?? displayedAgent?.backend ?? displayValue.agent_backend ?? null,
              model: displayValue.model ?? null,
              reasoning_effort: displayValue.reasoning_effort ?? null,
              ...changes,
            }
          : changes,
      );
    },
    [displayValue, displayedAgent, onChange, routeIsDefault],
  );

  const backend = displayValue.agent_backend || displayedAgent?.backend || '';
  const currentAgent = displayValue.agent_name;
  const currentModel = displayValue.model;
  const currentEffort = displayValue.reasoning_effort;
  // The closed trigger names the model as the menu does, from labels remembered
  // across pickers and loads; the raw id stays one hover away.
  const currentModelLabel = useRememberedModelLabel(backend, currentModel) ?? currentModel;
  // Fast mode is the route's to ask for and the model's hop's to grant: the mark
  // and the toggle follow whether this model would actually run fast, so asking
  // on a model without it keeps the choice for the next model that has it.
  const fastControls = compact && Boolean(onSpeedChange);
  const fastOn = speed === 'fast';
  // The model a turn runs on: the route's own, else its Agent's default, which a
  // Session that materialized its Agent at the first turn keeps without copying.
  const effectiveModel = currentModel || displayedAgent?.model || null;
  const fastOffered = useRememberedFastModel(backend, effectiveModel, fastControls && (fastOn || open));
  const fastActive = fastControls && fastOn && fastOffered;
  const currentAgentLabel = displayedAgent?.display_name || currentAgent;
  const triggerLabel = (routeIsDefault && defaultLabel) || currentAgentLabel || defaultLabel || t('chat.pickAgent');
  const agentLabel = currentAgentLabel || defaultLabel || t('chat.pickAgent');

  const allowedBackendSet = useMemo(
    () => new Set((allowedBackends ?? []).map((item) => item.trim()).filter(Boolean)),
    [allowedBackends],
  );
  const visibleAgents = useMemo(
    () => agents.filter(
      (agent) => agent.enabled && !agent.archived && (allowedBackendSet.size === 0 || allowedBackendSet.has(agent.backend)),
    ),
    [agents, allowedBackendSet],
  );

  useEffect(() => {
    const clearOpenCodeModels = () => {
      loadedModelBackendsRef.current.delete('opencode');
      setModelsByBackend((prev) => {
        if (!prev.opencode) return prev;
        const next = { ...prev };
        delete next.opencode;
        return next;
      });
      setModelLabelsByBackend((prev) => {
        if (!prev.opencode) return prev;
        const next = { ...prev };
        delete next.opencode;
        return next;
      });
    };
    window.addEventListener('avibe:opencode-model-options-changed', clearOpenCodeModels);
    return () => {
      window.removeEventListener('avibe:opencode-model-options-changed', clearOpenCodeModels);
    };
  }, []);

  // Settings covers this surface while it is open, and the Model Hub there may
  // change any backend's list, even which catalog supplies it. Nothing read
  // before the visit survives it: the return shows loading until a new read
  // lands, never a row the backend may no longer offer.
  useEffect(() => {
    if (surfaceActive) return;
    loadedModelBackendsRef.current.clear();
    hubBackendsRef.current.clear();
    setModelsByBackend({});
    setModelLabelsByBackend({});
    setReasoningByBackend({});
    setEffortMenusByBackend({});
  }, [surfaceActive]);

  const grouped = useMemo(() => {
    const groups: Record<string, VibeAgentBrief[]> = {};
    for (const agent of visibleAgents) {
      (groups[agent.backend] ||= []).push(agent);
    }
    return groups;
  }, [visibleAgents]);

  // Fetch the active backend's model list the first time the menu opens for it;
  // cached per backend so toggling agents doesn't refetch. A menu left open
  // under Settings reads again as it is uncovered. A Hub catalog is read again on
  // every open: its effort menu follows capability evidence, which any turn can
  // change, and the read is the persisted catalog, which starts no CLI. The last
  // answer shows until the new one lands, and its efforts and models (a model
  // choice keeps or clears the effort) are held while the read is in flight
  // (`readHubBackendModels`).
  useEffect(() => {
    const loadedBackends = loadedModelBackendsRef.current;
    if (!open || !surfaceActive || !backend || loadedBackends.has(backend)) return;
    loadedBackends.add(backend);
    let reloadOnNextOpen = true;
    setLoadingModels(true);
    const cancel = loadBackendModelsWithRefresh(
      api,
      backend,
      (result) => {
        const { models, modelLabels, reasoningOptions, effortMenus, catalogRefreshPending } = result;
        const knownHub = hubBackendsRef.current.has(backend);
        // A Hub answer marks the backend as Hub, a confirmed Direct one unmarks it
        // (back to the per-mount Direct cache); an unread Hub changes neither.
        if (effortMenus !== undefined) hubBackendsRef.current.add(backend);
        else if (!result.hubUnread) hubBackendsRef.current.delete(backend);
        reloadOnNextOpen = Boolean(catalogRefreshPending) || hubBackendsRef.current.has(backend);
        setLoadingModels(false);
        // A transient Hub failure answers with the native list: the last Hub answer stands.
        if (!supersedesHubAnswer(result, knownHub)) return;
        setReasoningByBackend((prev) => ({ ...prev, [backend]: reasoningOptions ?? {} }));
        setEffortMenusByBackend((prev) => ({ ...prev, [backend]: effortMenus ?? {} }));
        setModelsByBackend((prev) => ({ ...prev, [backend]: models }));
        setModelLabelsByBackend((prev) => ({ ...prev, [backend]: modelLabels ?? {} }));
        // Closed pickers anywhere name their model from this answer.
        rememberModelLabels(backend, modelLabels ?? {});
        // An unread Hub says nothing about which models run fast.
        if (!result.hubUnread) rememberFastModels(backend, result.fastModels ?? []);
      },
      () => {
        loadedBackends.delete(backend);
        setModelsByBackend((prev) => ({ ...prev, [backend]: [] }));
        // No catalog to consult: the backend's own ladder, as for any unknown model.
        setReasoningByBackend((prev) => ({ ...prev, [backend]: prev[backend] ?? {} }));
        setLoadingModels(false);
      },
    );
    return () => {
      cancel();
      setLoadingModels(false);
      if (reloadOnNextOpen) loadedBackends.delete(backend);
    };
  }, [open, surfaceActive, backend, api]);

  // Start each open (and every backend switch) from the full, unfiltered list.
  useEffect(() => {
    setModelQuery('');
  }, [backend, open]);

  const models = modelsByBackend[backend] ?? [];
  const modelLabels = modelLabelsByBackend[backend] ?? {};
  const backendReasoning = reasoningByBackend[backend] ?? EMPTY_REASONING_OPTIONS;
  const openAddModel = useAddModelExit(backend, open);
  // Show the search field only when the list is long enough to warrant it, so
  // claude/codex (a handful of models) stay uncluttered.
  const showModelSearch = models.length > 8;
  const trimmedQuery = modelQuery.trim().toLowerCase();
  const filteredModels = trimmedQuery
    ? models.filter(
        (m) =>
          m.toLowerCase().includes(trimmedQuery) ||
          modelOptionLabel(m, modelLabels).toLowerCase().includes(trimmedQuery),
      )
    : models;
  // Efforts are a catalog answer too. Until this backend's read has answered
  // (first open, or the read after a Settings visit dropped the last one), the
  // fallback ladder could offer an effort the current model rejects, so the
  // column waits with the model column instead.
  const catalogAnswered = Object.prototype.hasOwnProperty.call(reasoningByBackend, backend);
  const effortOptions = useMemo(
    () => (catalogAnswered ? resolveEffortOptions(backend, currentModel, backendReasoning) : []),
    [catalogAnswered, backend, currentModel, backendReasoning],
  );
  const effortMenus = effortMenusByBackend[backend];
  const effortNotOn = (currentModel && effortMenus?.[currentModel]?.not_on) || null;
  // Routing now refuses the saved effort on every hop: it is out of the menu, but
  // the route still sends it, so it shows as itself and says so.
  const refusedEffort = catalogAnswered ? refusedSavedEffort(currentEffort, currentModel, effortMenus) : null;
  // The layout and the column read one answer, so a refused-only column keeps its place.
  const showEffortColumn = effortOptions.length > 0 || refusedEffort !== null;
  // Names only when an effort needs one; a role that cannot read Sources gets the
  // hint without them.
  const sourceNames = useSourceNames(
    open && effortNotOn !== null && Object.keys(effortNotOn).length > 0,
    surfaceActive,
    authorization.instanceRole ?? 'none',
  );
  const notOnLabel = (effort: string): string | null => {
    // Efforts are free-form strings: only the map's own entry for this one counts,
    // never one inherited from Object.prototype (an effort named "constructor").
    const ids = effortNotOn && Object.prototype.hasOwnProperty.call(effortNotOn, effort) ? effortNotOn[effort] : null;
    if (!Array.isArray(ids) || ids.length === 0) return null;
    if (!sourceNames) return t('chat.picker.effortNotOnSome');
    return t('chat.picker.effortNotOn', { sources: ids.map((id) => sourceNames[id] ?? id).join(', ') });
  };

  // One set of picks for both presentations, so the compact menu cannot drift
  // from the cascade on what a click writes.
  const pickDefault = () =>
    applyPatch({
      agent_name: null,
      agent_id: null,
      agent_backend: null,
      agent_variant: null,
      model: null,
      reasoning_effort: null,
    });
  const pickAgent = (agent: VibeAgentBrief) =>
    applyPatch({
      agent_name: agent.name,
      agent_id: agent.id,
      agent_backend: agent.backend,
      agent_variant: agent.backend,
      // Explicit null (not undefined) so switching to an agent with
      // no default model/effort clears the previous override.
      model: agent.model ?? null,
      reasoning_effort: agent.reasoning_effort ?? null,
    });
  const pickModel = (model: string) => {
    const patch: AgentRoutePatch = { model };
    // Switching to a model whose effort set no longer includes
    // the current effort: clear it in the same patch so the displayed
    // route matches what dispatches. Choosing the current model
    // again changes nothing, a refused saved effort included.
    if (model !== currentModel && !isEffortSupported(backend, model, currentEffort, backendReasoning)) {
      patch.reasoning_effort = null;
    }
    applyPatch(patch);
  };
  const openNewAgent = () => {
    setOpen(false);
    onNavigateAway?.(); // close the create sheet before leaving, if any
    // Creating an agent needs the Definitions controls, so ask for
    // that tab explicitly instead of resuming the remembered one.
    navigate('/agents?tab=definitions');
  };
  const openAddModelExit = openAddModel
    ? () => {
        setOpen(false);
        openAddModel();
      }
    : null;
  const effortLabel = (effort: string) => t(`chat.picker.effortOptions.${effort}`, { defaultValue: effort });

  // The model list body — search, loading/empty states, rows — is shared; only
  // how a row presents its name differs.
  const modelListBody = (renderRow: (model: string) => React.ReactNode) => (
    <>
      {showModelSearch && (
        <div className="sticky top-0 z-10 -mx-1.5 mb-1 bg-panel px-1.5 pb-1 pt-0.5">
          <div className="flex items-center gap-1.5 rounded-md border border-border bg-surface-2 px-2 focus-within:border-cyan/50">
            <Search className="size-3 shrink-0 text-muted" />
            <Input
              variant="bare"
              value={modelQuery}
              onChange={(e) => setModelQuery(e.target.value)}
              placeholder={t('chat.picker.searchModels')}
              aria-label={t('chat.picker.searchModels')}
              className="h-7 flex-1 text-[11px]"
            />
          </div>
        </div>
      )}
      {loadingModels && models.length === 0 ? (
        <div className="flex items-center gap-1.5 px-2 py-3 text-[11px] text-muted">
          <Loader2 className="size-3 animate-spin" />
          {t('common.loading')}
        </div>
      ) : models.length === 0 ? (
        <div className="px-2 py-3 text-[11px] text-muted">{t('chat.picker.noModels')}</div>
      ) : filteredModels.length === 0 ? (
        <div className="px-2 py-3 text-[11px] text-muted">{t('chat.picker.noModelMatch')}</div>
      ) : (
        filteredModels.map(renderRow)
      )}
      {/* Pinned like the search above it: a long model list must not scroll
          the way to add one out of sight. */}
      {openAddModelExit && (
        <div className="sticky -bottom-1.5 z-10 -mx-1.5 -mb-1.5 border-t border-border bg-panel px-1.5 pb-1.5">
          <PickerExitButton label={t('chat.picker.addModel')} onClick={openAddModelExit} />
        </div>
      )}
    </>
  );

  const triggerButton = (
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          className={clsx(
            compact
              ? 'inline-flex h-9 min-w-0 items-center justify-start gap-1.5 rounded-lg border-transparent bg-transparent px-2 text-[12px] font-normal shadow-none hover:bg-foreground/[0.05]'
              : 'inline-flex h-auto items-center justify-start gap-1.5 rounded-lg border-cyan/40 bg-surface-2 px-2.5 py-1.5 text-[12px] font-normal hover:bg-cyan/[0.06]',
            triggerClassName ?? (compact ? undefined : 'max-w-[62%]'),
          )}
        >
          {/* The composer's quiet trigger names the route without the backend
              badge: the Agent name already implies it, and the menu shows it. */}
          {backend && !compact && (
            <span className="inline-flex shrink-0 items-center gap-1 rounded border border-cyan/30 bg-cyan/[0.08] px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase text-cyan-ink">
              <BackendAvatar backend={backend} className="size-3" />
              {getBackendUiMeta(backend).label}
            </span>
          )}
          {compact ? (
            /* The composer's quiet trigger names who answers; whether that Agent is
               inherited from the default is the menu's Agent row to say. When the
               action row runs short (a phone with Queue and Stop showing), detail
               gives way before the Agent: effort first, then the model. Each detail
               part that no longer fits wraps onto a second line this one-line box
               clips, so a detail is either whole or gone — never an ellipsis. */
            <span
              data-route-trigger-parts=""
              className="flex h-5 min-w-0 flex-wrap content-start items-center justify-end gap-x-1.5 overflow-hidden"
            >
              {/* The name keeps its line: detail wraps away first, and only a name
                  wider than the whole row, or than 10rem, ends in an ellipsis. */}
              <span className="h-5 min-w-0 max-w-40 truncate font-semibold leading-5 text-foreground">{agentLabel}</span>
              {currentModel && (
                <span
                  data-route-detail="model"
                  className="h-5 shrink-0 whitespace-nowrap font-mono text-[10px] leading-5 text-muted before:mr-1.5 before:font-sans before:content-['·']"
                  title={currentModelLabel !== currentModel ? currentModel : undefined}
                >
                  {currentModelLabel}
                </span>
              )}
              {currentEffort && (
                <span
                  data-route-detail="effort"
                  className="h-5 shrink-0 whitespace-nowrap text-[10px] capitalize leading-5 text-muted before:mr-1.5 before:content-['·']"
                >
                  {t(`chat.picker.effortOptions.${currentEffort}`, { defaultValue: currentEffort })}
                </span>
              )}
            </span>
          ) : (
            <>
              <span className="truncate font-semibold text-foreground">{triggerLabel}</span>
              {currentModel && (
                <>
                  <span className="text-muted">·</span>
                  <span
                    className="truncate font-mono text-[10px] text-muted"
                    title={currentModelLabel !== currentModel ? currentModel : undefined}
                  >
                    {currentModelLabel}
                  </span>
                </>
              )}
              {currentEffort && (
                <>
                  <span className="text-muted">·</span>
                  {/* Localize via the same key the column uses so the closed trigger
                      doesn't show raw low/max in zh builds; unknown falls back to raw. */}
                  <span className="shrink-0 text-[10px] capitalize text-muted">
                    {t(`chat.picker.effortOptions.${currentEffort}`, { defaultValue: currentEffort })}
                  </span>
                </>
              )}
            </>
          )}
          {/* In-flight feedback replaces the chevron rather than greying the menu:
              the pick is already applied, so the user is watching a save, not
              waiting for their click to take effect. */}
          <span className="ml-auto flex shrink-0 items-center gap-0.5">
            {/* Outside the wrapping parts, so the mark never gives way: it says the
                route costs more, which a narrow row must not hide. */}
            {fastActive && <FastMark data-fast-mark="" aria-label={t('chat.picker.fastOn')} className="text-gold/60" />}
            {saving ? (
              <Loader2 className="size-3 shrink-0 animate-spin text-muted" aria-label={t('common.saving')} />
            ) : (
              <ChevronDown className="size-3 shrink-0 text-muted" />
            )}
          </span>
        </Button>
  );

  if (compact) {
    const agentRowLabel = (routeIsDefault && defaultLabel) || agentLabel;
    const agentList = (
      <div className="flex flex-col">
        <div className="border-b border-border p-1.5">
          <button
            type="button"
            onClick={() => setAgentPage(false)}
            className="flex w-full items-center gap-1 rounded px-1.5 py-1.5 text-left text-[12px] font-semibold text-foreground hover:bg-foreground/[0.04]"
          >
            <ChevronLeft className="size-3.5 text-muted" />
            {t('chat.picker.agent')}
          </button>
        </div>
        {/* The popover has no height of its own, so the list bounds and scrolls
            itself on desktop with the way back pinned above it; the phone sheet
            already scrolls its whole body. */}
        <div className="flex flex-col gap-0.5 p-1.5 md:max-h-[min(60vh,420px)] md:overflow-y-auto">
          {defaultLabel && (
            <RouteItem
              active={routeIsDefault}
              onClick={() => {
                pickDefault();
                setAgentPage(false);
              }}
            >
              <Sparkles className="size-3.5 shrink-0 text-cyan-ink" />
              <span className="flex-1 truncate font-semibold">{defaultLabel}</span>
              {routeIsDefault && <Check className="size-3.5 shrink-0" />}
            </RouteItem>
          )}
          {visibleAgents.length === 0 && (
            <div className="px-2 py-3 text-center text-[11px] text-muted">{t('chat.noAgents')}</div>
          )}
          {Object.entries(grouped).map(([be, list]) => (
            <div key={be} className="flex flex-col gap-0.5 pb-1">
              <div className="px-2 pt-1.5 font-mono text-[9px] font-bold uppercase tracking-[0.12em] text-muted">{be}</div>
              {list.map((agent) => {
                const active = !routeIsDefault && agent.name === currentAgent;
                return (
                  <RouteItem
                    key={agent.id}
                    active={active}
                    onClick={() => {
                      pickAgent(agent);
                      setAgentPage(false);
                    }}
                  >
                    <BackendAvatar backend={agent.backend} className="size-3.5 shrink-0" />
                    <span className="flex-1 truncate font-semibold">{agent.name}</span>
                    {agent.model && <span className="truncate font-mono text-[9px] text-muted">{agent.model}</span>}
                    {active && <Check className="size-3.5 shrink-0" />}
                  </RouteItem>
                );
              })}
            </div>
          ))}
          <PickerExitButton label={t('chat.picker.newAgent')} onClick={openNewAgent} />
        </div>
      </div>
    );
    const routeMenu = (
      <div className="flex flex-col">
        <button
          type="button"
          onClick={() => setAgentPage(true)}
          className="flex items-center gap-2 px-3 py-2.5 text-left transition-colors hover:bg-foreground/[0.03]"
        >
          <span className="text-[10px] font-bold uppercase tracking-[0.1em] text-muted">{t('chat.picker.agent')}</span>
          <span className="ml-auto flex min-w-0 items-center gap-1.5">
            {backend && <BackendAvatar backend={backend} className="size-3.5 shrink-0" />}
            <span className="truncate text-[12px] font-semibold text-foreground">{agentRowLabel}</span>
            <ChevronRight className="size-3.5 shrink-0 text-muted" />
          </span>
        </button>
        <RouteColumn title={t('chat.picker.model')} className="max-h-[40vh] border-t border-border">
          {modelListBody((model) => {
            const label = modelOptionLabel(model, modelLabels);
            const active = model === currentModel;
            return (
              <RouteItem key={model} active={active} disabled={loadingModels} onClick={() => pickModel(model)}>
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="whitespace-normal break-words font-semibold leading-snug">
                    <ModelName label={label} />
                  </span>
                  {label !== model && (
                    <span className="whitespace-normal break-words font-mono text-[10px] leading-snug text-muted">{model}</span>
                  )}
                </span>
                {active && <Check className="size-3.5 shrink-0" />}
              </RouteItem>
            );
          })}
        </RouteColumn>
        {(showEffortColumn || fastControls) && (
          <div className="border-t border-border p-3">
            <div className={clsx('flex items-center', showEffortColumn && 'pb-2')}>
              <span className="text-[10px] font-bold uppercase tracking-[0.1em] text-muted">
                {showEffortColumn ? t('chat.picker.effort') : t('chat.picker.speed')}
              </span>
              {fastControls && (
                <FastToggle
                  on={fastOn}
                  offered={fastOffered}
                  active={fastActive}
                  disabled={loadingModels}
                  onToggle={() => onSpeedChange?.(fastOn ? null : 'fast')}
                />
              )}
            </div>
            {showEffortColumn && (
            <>
            {/* One tap per effort, wrapping when a model states many. Hints that
                a column would print under each row are listed below instead. */}
            <div
              role="radiogroup"
              aria-label={t('chat.picker.effort')}
              className="flex flex-wrap gap-0.5 rounded-md border border-border bg-foreground/[0.03] p-0.5"
            >
              {refusedEffort && (
                <EffortChip active refused label={effortLabel(refusedEffort)} onClick={() => setOpen(false)} />
              )}
              {effortOptions.map((opt) => (
                <EffortChip
                  key={opt}
                  active={opt === currentEffort}
                  disabled={loadingModels}
                  label={effortLabel(opt)}
                  hint={notOnLabel(opt)}
                  onClick={() => applyPatch({ reasoning_effort: opt })}
                />
              ))}
            </div>
            {refusedEffort && (
              <p className="pt-1.5 text-[10px] leading-snug text-gold-ink">{t('chat.picker.effortRefused')}</p>
            )}
            {effortOptions.map((opt) => {
              const notOn = notOnLabel(opt);
              return notOn ? (
                <p key={opt} className="flex gap-1.5 pt-1 text-[10px] leading-snug text-muted">
                  <span className="shrink-0 font-semibold">{effortLabel(opt)}</span>
                  <span>{notOn}</span>
                </p>
              ) : null;
            })}
            </>
            )}
            {/* A touch screen has no hover for the toggle's explanation: it is
                written out while the route runs fast. */}
            {fastActive && (
              <p className="pt-1.5 text-[10px] leading-snug text-muted [@media(hover:hover)]:hidden">
                {t('chat.picker.fastCaption')}
              </p>
            )}
          </div>
        )}
      </div>
    );
    return (
      <ResponsiveMenu
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) setAgentPage(false);
        }}
        trigger={triggerButton}
        sheetTitle={t('chat.picker.sheetTitle')}
        sheetCloseLabel={t('common.done')}
        className="w-[320px] max-w-[92vw] overflow-hidden p-0"
        align={align}
        modal={modal}
      >
        {agentPage ? agentList : routeMenu}
      </ResponsiveMenu>
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen} modal={modal}>
      <PopoverTrigger asChild>
        {triggerButton}
      </PopoverTrigger>
      <PopoverContent align={align} className="z-50 max-h-[70vh] w-[680px] max-w-[92vw] overflow-y-auto p-0">
        {/* On phones the columns stack into one scrollable list; sm+ keeps the
            side-by-side cascading menu. Model gets the widest track (long ids)
            and Effort the narrowest (short labels). When the picked model states
            no efforts the menu is a two-column cascade: an Effort column with a
            heading and nothing under it would look like a list that failed. */}
        <div
          className={clsx(
            'grid grid-cols-1 divide-y divide-border sm:divide-x sm:divide-y-0',
            showEffortColumn ? 'sm:grid-cols-[1fr_1.35fr_0.7fr]' : 'sm:grid-cols-[1fr_1.35fr]',
          )}
        >
          {/* Column 1 — Agent */}
          <RouteColumn title={t('chat.picker.agent')}>
            {/* Default option: clears the route back to inherited defaults after
                picking an explicit agent, without a page refresh. */}
            {defaultLabel && (
              <RouteItem
                active={routeIsDefault}
                onClick={pickDefault}
              >
                <Sparkles className="size-3.5 shrink-0 text-cyan-ink" />
                <span className="flex-1 truncate font-semibold">{defaultLabel}</span>
              </RouteItem>
            )}
            {visibleAgents.length === 0 && (
              <div className="px-2 py-3 text-center text-[11px] text-muted">{t('chat.noAgents')}</div>
            )}
            {Object.entries(grouped).map(([be, list]) => (
              <div key={be} className="flex flex-col gap-0.5 pb-1">
                <div className="px-2 pt-1.5 font-mono text-[9px] font-bold uppercase tracking-[0.12em] text-muted">{be}</div>
                {list.map((agent) => (
                  <RouteItem
                    key={agent.id}
                    active={!routeIsDefault && agent.name === currentAgent}
                    onClick={() => pickAgent(agent)}
                  >
                    <span className="flex-1 truncate font-semibold">{agent.name}</span>
                    {agent.model && <span className="truncate font-mono text-[9px] text-muted">{agent.model}</span>}
                  </RouteItem>
                ))}
              </div>
            ))}
            <PickerExitButton label={t('chat.picker.newAgent')} onClick={openNewAgent} />
          </RouteColumn>

          {/* Column 2 — Model (lazy-loaded for the active backend). Its own
              scroll box on phones too, where the columns stack: the sticky search
              and Add model rows pin to the nearest scroll box, and a column that
              never scrolls would leave them at the far ends of a long list. */}
          <RouteColumn title={t('chat.picker.model')} className="max-h-[45vh]">
            {modelListBody((model) => (
              <RouteItem
                key={model}
                active={model === currentModel}
                disabled={loadingModels}
                onClick={() => pickModel(model)}
              >
                {/* Full id always visible (wraps instead of truncating); the
                    provider/vendor prefix on OpenCode ids is de-emphasized so the
                    model name itself stands out. */}
                <span className="min-w-0 flex-1 whitespace-normal break-words text-left font-mono text-[11px] leading-snug">
                  <ModelName label={modelOptionLabel(model, modelLabels)} />
                </span>
              </RouteItem>
            ))}
          </RouteColumn>

          {/* Column 3 — Effort (uses per-model catalog entries when available) */}
          {showEffortColumn && (
            <RouteColumn title={t('chat.picker.effort')}>
              {refusedEffort && (
                <RouteItem active onClick={() => setOpen(false)}>
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="capitalize">{effortLabel(refusedEffort)}</span>
                    <span className="text-[10px] leading-snug text-gold-ink">{t('chat.picker.effortRefused')}</span>
                  </span>
                </RouteItem>
              )}
              {effortOptions.map((opt) => {
                const notOn = notOnLabel(opt);
                return (
                  <RouteItem
                    key={opt}
                    active={opt === currentEffort}
                    disabled={loadingModels}
                    onClick={() => applyPatch({ reasoning_effort: opt })}
                  >
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="capitalize">{effortLabel(opt)}</span>
                      {notOn && <span className="text-[10px] leading-snug text-muted">{notOn}</span>}
                    </span>
                  </RouteItem>
                );
              })}
            </RouteColumn>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
};

// One effort in the compact menu's radio group. A refused saved effort stays
// checked and gold: routing still sends it, so it shows as itself.
const EffortChip: React.FC<{
  active: boolean;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  hint?: string | null;
  refused?: boolean;
}> = ({ active, label, onClick, disabled, hint, refused }) => (
  <button
    type="button"
    role="radio"
    aria-checked={active}
    disabled={disabled}
    title={hint ?? undefined}
    onClick={onClick}
    className={clsx(
      'min-w-[3rem] flex-1 whitespace-nowrap rounded-[4px] border px-2 py-1.5 text-[12px] capitalize transition-colors',
      'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan/60 disabled:cursor-not-allowed disabled:opacity-60',
      refused
        ? 'border-gold/40 bg-gold/10 font-bold text-gold-ink'
        : active
          ? 'border-cyan/40 bg-cyan-soft font-bold text-cyan-ink'
          : 'border-transparent font-medium text-muted hover:text-foreground',
      hint && !active && 'underline decoration-dotted underline-offset-4',
    )}
  >
    {label}
  </button>
);

const RouteColumn: React.FC<{ title: string; className?: string; children: React.ReactNode }> = ({ title, className, children }) => (
  <div className={clsx('flex min-w-0 flex-col overflow-y-auto p-1.5 sm:max-h-[320px]', className)}>
    <div className="px-2 pb-1 pt-0.5 text-[10px] font-bold uppercase tracking-[0.1em] text-muted">{title}</div>
    {children}
  </div>
);

// The "+ …" row closing a column: it leaves the picker for the page where that
// column's list is edited.
const PickerExitButton: React.FC<{ label: string; onClick: () => void }> = ({ label, onClick }) => (
  <Button
    type="button"
    variant="ghost"
    size="sm"
    onClick={onClick}
    className="mt-1 h-auto w-full justify-start gap-1.5 rounded px-2 py-1.5 text-[11px] font-medium text-cyan-ink hover:bg-cyan/[0.08] hover:text-cyan-ink"
  >
    <Plus className="size-3.5" />
    {label}
  </Button>
);

// A picker row on the shared Button primitive (variant + className overrides)
// so it inherits the design system's focus/disabled behavior.
const RouteItem: React.FC<{
  active: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}> = ({ active, disabled, onClick, children }) => (
  <Button
    type="button"
    variant="ghost"
    size="sm"
    disabled={disabled}
    onClick={onClick}
    className={clsx(
      'h-auto w-full justify-start gap-2 rounded px-2 py-1.5 text-left text-[12px] font-normal',
      active ? 'bg-cyan/[0.10] text-cyan-ink hover:bg-cyan/[0.10] hover:text-cyan-ink' : 'text-foreground hover:bg-foreground/[0.04]',
    )}
  >
    {children}
  </Button>
);

// Renders a model id with its OpenCode-style `provider/vendor/` prefix muted so
// the model name (the last path segment, what actually differs) reads first.
// No `/` (claude / codex) → the whole id shows plainly.
const ModelName: React.FC<{ label: string }> = ({ label }) => {
  const cut = label.lastIndexOf('/');
  if (cut < 0) return <>{label}</>;
  return (
    <>
      <span className="text-muted">{label.slice(0, cut + 1)}</span>
      {label.slice(cut + 1)}
    </>
  );
};

// The fast-mode bolt: a slim filled glyph, quiet enough to sit beside the
// chevron without competing with the route's name. Colour comes from the caller.
const FAST_BOLT_PATH = 'M13.6 2.2 6.4 13.1h5l-1.5 8.7 7.7-11.4h-5.2z';

const FastMark: React.FC<React.SVGProps<SVGSVGElement>> = ({ className, ...props }) => (
  <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden={props['aria-label'] ? undefined : true} className={clsx('size-[11px] shrink-0', className)} {...props}>
    <path d={FAST_BOLT_PATH} />
  </svg>
);

// The Fast toggle beside the effort heading. Hover or keyboard focus explains
// it; a click's focus does not, so the tip never lingers over the model list. A
// model whose hop offers no fast tier keeps the choice but cannot change it here.
const FastToggle: React.FC<{
  on: boolean;
  offered: boolean;
  active: boolean;
  disabled?: boolean;
  onToggle: () => void;
}> = ({ on, offered, active, disabled, onToggle }) => {
  const { t } = useTranslation();
  const tipId = useId();
  return (
    <span className="group/fast relative ml-auto">
      <button
        type="button"
        aria-pressed={on}
        aria-describedby={tipId}
        disabled={disabled || !offered}
        onClick={onToggle}
        className={clsx(
          'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold transition-colors',
          'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-cyan/60 disabled:cursor-not-allowed disabled:opacity-50',
          active ? 'border-gold/30 bg-gold-soft text-gold-ink/80' : 'border-border text-muted hover:text-foreground',
        )}
      >
        <FastMark className={active ? 'text-gold/60' : undefined} />
        {t('chat.picker.fast')}
      </button>
      <span
        id={tipId}
        role="tooltip"
        className="pointer-events-none absolute bottom-full right-0 z-50 mb-1.5 w-56 rounded-md bg-foreground px-2.5 py-2 text-[11px] leading-snug text-background opacity-0 shadow-md transition-opacity group-hover/fast:opacity-100 group-has-[:focus-visible]/fast:opacity-100"
      >
        <span className="block font-semibold">{t('chat.picker.fastTitle')}</span>
        <span className="block">{t('chat.picker.fastDescription')}</span>
        <span className="block text-background/65">
          {offered ? t('chat.picker.fastScope') : t('chat.picker.fastUnavailable')}
        </span>
      </span>
    </span>
  );
};
