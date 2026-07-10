import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { Bot, ChevronDown, Loader2, Plus, Search, Sparkles } from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import type { VibeAgentBrief } from '../../context/ApiContext';
import { loadBackendModelsWithRefresh, modelOptionLabel } from '../../lib/backendModels';
import { isEffortSupported, resolveEffortOptions } from '../../lib/effortOptions';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { Button } from '../ui/button';
import { Input } from '../ui/input';

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
  onChange: (patch: AgentRoutePatch) => void | Promise<void>;
  /** Optional backend allow-list for existing sessions whose backend is pinned. */
  allowedBackends?: string[];
  /** Trigger label when no agent is selected — the create flow shows "默认 · …". */
  defaultLabel?: string;
  /** Concrete route to display while `value` is inheriting a project/global default. */
  defaultRoute?: AgentRouteValue;
  /** True when the displayed route is inherited, not persisted on this session/project yet. */
  isDefaultRoute?: boolean;
  disabled?: boolean;
  align?: 'start' | 'end';
  /** Override the trigger width (chat caps at 62%; the create surfaces go full width). */
  triggerClassName?: string;
  /** On mobile chat headers, keep the closed trigger to the agent label only. */
  compactMobile?: boolean;
  /** Make the popover a modal layer — required when nested in a modal Dialog (the
   *  new-session sheet) so its content isn't aria-hidden/inert by the dialog. */
  modal?: boolean;
  /** Called before navigating to /agents — the create sheet uses it to close
   *  itself so the destination isn't left behind a focus-trapped modal. */
  onNavigateAway?: () => void;
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
  disabled,
  align = 'end',
  triggerClassName,
  compactMobile,
  modal,
  onNavigateAway,
}) => {
  const { t } = useTranslation();
  const api = useApi();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [modelsByBackend, setModelsByBackend] = useState<Record<string, string[]>>({});
  const [modelLabelsByBackend, setModelLabelsByBackend] = useState<Record<string, Record<string, string>>>({});
  const [reasoningByBackend, setReasoningByBackend] = useState<
    Record<string, Record<string, ReasoningOption[]>>
  >({});
  const loadedModelBackendsRef = useRef<Set<string>>(new Set());
  const [loadingModels, setLoadingModels] = useState(false);
  const [patching, setPatching] = useState(false);
  // Free-text filter for the model column — long backends (OpenCode) list dozens.
  const [modelQuery, setModelQuery] = useState('');

  const hasExplicitRoute = Boolean(value.agent_name || value.agent_backend);
  const routeIsDefault = isDefaultRoute ?? !hasExplicitRoute;
  const displayValue = routeIsDefault ? (defaultRoute ?? value) : value;
  const displayedAgent = displayValue.agent_name
    ? agents.find((agent) => agent.name === displayValue.agent_name) || null
    : null;

  // Serialize patches: an agent pick carries its default model/effort, so if it
  // resolves AFTER a later model/effort pick the later choice would be rolled
  // back. One patch at a time, items disabled while it's in flight.
  const applyPatch = useCallback(
    async (changes: AgentRoutePatch) => {
      if (patching) return;
      setPatching(true);
      try {
        const patchPinsDisplayedDefault =
          routeIsDefault &&
          Boolean(displayValue.agent_name || displayValue.agent_backend) &&
          !('agent_name' in changes) &&
          !('agent_backend' in changes) &&
          !('agent_id' in changes) &&
          !('agent_variant' in changes);
        await onChange(
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
      } finally {
        setPatching(false);
      }
    },
    [displayValue, displayedAgent, patching, onChange, routeIsDefault],
  );

  const backend = displayValue.agent_backend || displayedAgent?.backend || '';
  const currentAgent = displayValue.agent_name;
  const currentModel = displayValue.model;
  const currentEffort = displayValue.reasoning_effort;
  const triggerLabel = (routeIsDefault && defaultLabel) || currentAgent || defaultLabel || t('chat.pickAgent');
  const compactTriggerLabel = currentAgent || defaultLabel || t('chat.pickAgent');

  const allowedBackendSet = useMemo(
    () => new Set((allowedBackends ?? []).map((item) => item.trim()).filter(Boolean)),
    [allowedBackends],
  );
  const visibleAgents = useMemo(
    () => (allowedBackendSet.size === 0 ? agents : agents.filter((agent) => allowedBackendSet.has(agent.backend))),
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

  const grouped = useMemo(() => {
    const groups: Record<string, VibeAgentBrief[]> = {};
    for (const agent of visibleAgents) {
      (groups[agent.backend] ||= []).push(agent);
    }
    return groups;
  }, [visibleAgents]);

  // Fetch the active backend's model list the first time the menu opens for it;
  // cached per backend so toggling agents doesn't refetch.
  useEffect(() => {
    const loadedBackends = loadedModelBackendsRef.current;
    if (!open || !backend || loadedBackends.has(backend)) return;
    loadedBackends.add(backend);
    let reloadOnNextOpen = true;
    setLoadingModels(true);
    const cancel = loadBackendModelsWithRefresh(
      api,
      backend,
      ({ models, modelLabels, reasoningOptions, catalogRefreshPending }) => {
        reloadOnNextOpen = Boolean(catalogRefreshPending);
        setReasoningByBackend((prev) => ({ ...prev, [backend]: reasoningOptions ?? {} }));
        setModelsByBackend((prev) => ({ ...prev, [backend]: models }));
        setModelLabelsByBackend((prev) => ({ ...prev, [backend]: modelLabels ?? {} }));
        setLoadingModels(false);
      },
      () => {
        loadedBackends.delete(backend);
        setModelsByBackend((prev) => ({ ...prev, [backend]: [] }));
        setLoadingModels(false);
      },
    );
    return () => {
      cancel();
      if (reloadOnNextOpen) loadedBackends.delete(backend);
    };
  }, [open, backend, api]);

  // Start each open (and every backend switch) from the full, unfiltered list.
  useEffect(() => {
    setModelQuery('');
  }, [backend, open]);

  const models = modelsByBackend[backend] ?? [];
  const modelLabels = modelLabelsByBackend[backend] ?? {};
  const backendReasoning = reasoningByBackend[backend] ?? EMPTY_REASONING_OPTIONS;
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
  const effortOptions = useMemo(
    () => resolveEffortOptions(backend, currentModel, backendReasoning),
    [backend, currentModel, backendReasoning],
  );

  return (
    <Popover open={open} onOpenChange={setOpen} modal={modal}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          disabled={disabled}
          className={clsx(
            'inline-flex h-auto items-center justify-start gap-1.5 rounded-lg border-cyan/40 bg-surface-2 px-2.5 py-1.5 text-[12px] font-normal hover:bg-cyan/[0.06]',
            triggerClassName ?? 'max-w-[62%]',
          )}
        >
          {backend && (
            <span
              className={clsx(
                'shrink-0 items-center gap-1 rounded border border-cyan/30 bg-cyan/[0.08] px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase text-cyan',
                compactMobile ? 'hidden md:inline-flex' : 'inline-flex',
              )}
            >
              <Bot className="size-3" />
              {backend}
            </span>
          )}
          {compactMobile ? (
            <>
              <span className="truncate font-semibold text-foreground md:hidden">{compactTriggerLabel}</span>
              <span className="hidden truncate font-semibold text-foreground md:inline">{triggerLabel}</span>
            </>
          ) : (
            <span className="truncate font-semibold text-foreground">{triggerLabel}</span>
          )}
          {currentModel && (
            <>
              <span className={clsx('text-muted', compactMobile && 'hidden md:inline')}>·</span>
              <span className={clsx('truncate font-mono text-[10px] text-muted', compactMobile && 'hidden md:inline')}>
                {currentModel}
              </span>
            </>
          )}
          {currentEffort && (
            <>
              <span className={clsx('text-muted', compactMobile && 'hidden md:inline')}>·</span>
              {/* Localize via the same key the column uses so the closed trigger
                  doesn't show raw low/max in zh builds; unknown falls back to raw. */}
              <span className={clsx('shrink-0 text-[10px] capitalize text-muted', compactMobile && 'hidden md:inline')}>
                {t(`chat.picker.effortOptions.${currentEffort}`, { defaultValue: currentEffort })}
              </span>
            </>
          )}
          <ChevronDown className="ml-auto size-3 shrink-0 text-muted" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align={align} className="z-50 max-h-[70vh] w-[680px] max-w-[92vw] overflow-y-auto p-0">
        {/* On phones the three columns stack into one scrollable list; sm+ keeps
            the side-by-side cascading menu. Model gets the widest track (long ids)
            and Effort the narrowest (short labels). */}
        <div className="grid grid-cols-1 divide-y divide-border sm:grid-cols-[1fr_1.35fr_0.7fr] sm:divide-x sm:divide-y-0">
          {/* Column 1 — Agent */}
          <RouteColumn title={t('chat.picker.agent')}>
            {/* Default option: clears the route back to inherited defaults after
                picking an explicit agent, without a page refresh. */}
            {defaultLabel && (
              <RouteItem
                active={routeIsDefault}
                disabled={patching}
                onClick={() =>
                  void applyPatch({
                    agent_name: null,
                    agent_id: null,
                    agent_backend: null,
                    agent_variant: null,
                    model: null,
                    reasoning_effort: null,
                  })
                }
              >
                <Sparkles className="size-3.5 shrink-0 text-cyan" />
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
                    disabled={patching}
                    onClick={() =>
                      void applyPatch({
                        agent_name: agent.name,
                        agent_id: agent.id,
                        agent_backend: agent.backend,
                        agent_variant: agent.backend,
                        // Explicit null (not undefined) so switching to an agent with
                        // no default model/effort clears the previous override.
                        model: agent.model ?? null,
                        reasoning_effort: agent.reasoning_effort ?? null,
                      })
                    }
                  >
                    <span className="flex-1 truncate font-semibold">{agent.name}</span>
                    {agent.model && <span className="truncate font-mono text-[9px] text-muted">{agent.model}</span>}
                  </RouteItem>
                ))}
              </div>
            ))}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                setOpen(false);
                onNavigateAway?.(); // close the create sheet before leaving, if any
                navigate('/agents');
              }}
              className="mt-1 h-auto w-full justify-start gap-1.5 rounded px-2 py-1.5 text-[11px] font-medium text-cyan hover:bg-cyan/[0.08] hover:text-cyan"
            >
              <Plus className="size-3.5" />
              {t('chat.picker.newAgent')}
            </Button>
          </RouteColumn>

          {/* Column 2 — Model (lazy-loaded for the active backend) */}
          <RouteColumn title={t('chat.picker.model')}>
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
              filteredModels.map((model) => (
                <RouteItem
                  key={model}
                  active={model === currentModel}
                  disabled={patching}
                  onClick={() => {
                    const patch: AgentRoutePatch = { model };
                    // Switching to a model whose effort set no longer includes
                    // the current effort: clear it in the same patch so the displayed
                    // route matches what dispatches.
                    if (!isEffortSupported(backend, model, currentEffort, backendReasoning)) {
                      patch.reasoning_effort = null;
                    }
                    void applyPatch(patch);
                  }}
                >
                  {/* Full id always visible (wraps instead of truncating); the
                      provider/vendor prefix on OpenCode ids is de-emphasized so the
                      model name itself stands out. */}
                  <span className="min-w-0 flex-1 whitespace-normal break-words text-left font-mono text-[11px] leading-snug">
                    <ModelName label={modelOptionLabel(model, modelLabels)} />
                  </span>
                </RouteItem>
              ))
            )}
          </RouteColumn>

          {/* Column 3 — Effort (uses per-model catalog entries when available) */}
          <RouteColumn title={t('chat.picker.effort')}>
            {effortOptions.map((opt) => (
              <RouteItem
                key={opt}
                active={opt === currentEffort}
                disabled={patching}
                onClick={() => void applyPatch({ reasoning_effort: opt })}
              >
                <span className="flex-1 capitalize">
                  {t(`chat.picker.effortOptions.${opt}`, { defaultValue: opt })}
                </span>
              </RouteItem>
            ))}
          </RouteColumn>
        </div>
      </PopoverContent>
    </Popover>
  );
};

const RouteColumn: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div className="flex min-w-0 flex-col overflow-y-auto p-1.5 sm:max-h-[320px]">
    <div className="px-2 pb-1 pt-0.5 text-[10px] font-bold uppercase tracking-[0.1em] text-muted">{title}</div>
    {children}
  </div>
);

// A picker row on the shared Button primitive (variant + className overrides)
// so it inherits the design system's focus/disabled behavior.
const RouteItem: React.FC<{
  active: boolean;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}> = ({ active, onClick, disabled, children }) => (
  <Button
    type="button"
    variant="ghost"
    size="sm"
    onClick={onClick}
    disabled={disabled}
    className={clsx(
      'h-auto w-full justify-start gap-2 rounded px-2 py-1.5 text-left text-[12px] font-normal',
      active ? 'bg-cyan/[0.10] text-cyan hover:bg-cyan/[0.10] hover:text-cyan' : 'text-foreground hover:bg-foreground/[0.04]',
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
