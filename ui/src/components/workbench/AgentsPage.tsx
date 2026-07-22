import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Bot,
  ChevronDown,
  ChevronRight,
  FileText,
  Funnel,
  Loader2,
  Maximize2,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  Search,
  Star,
  Trash2,
  Upload,
  Activity,
  Layers,
} from 'lucide-react';
import clsx from 'clsx';

import { useApi } from '../../context/ApiContext';
import type { VibeAgentBrief, VibeAgentFull } from '../../context/ApiContext';
import { AgentGraphTab } from './AgentGraphTab';
import { useToast } from '../../context/ToastContext';
import { NewAgentDialog } from './NewAgentDialog';
import { RunAgentDialog } from './RunAgentDialog';
import { GlobalPromptsDialog } from './GlobalPromptsDialog';
import { Badge } from '../ui/badge';
import { Button } from '../ui/button';
import { Switch } from '../ui/switch';
import { Combobox } from '../ui/combobox';
import type { ComboboxOption } from '../ui/combobox';
import { Textarea } from '../ui/textarea';
import { EditorDialog } from '../ui/editor-dialog';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { estimateTokens } from '../../lib/tokenEstimate';
import { loadBackendModelsWithRefresh, modelOptionLabel } from '../../lib/backendModels';
import { resolveEffortOptions } from '../../lib/effortOptions';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';
import { CapabilityTabs } from './CapabilityTabs';
// Backend order / labels / accent classes live in lib/backendAccent, shared
// with the Skills surface (BACKEND_TEXT is this page's old BACKEND_ICON_CLASS).
import {
  BACKEND_ORDER,
  BACKEND_LABEL,
  BACKEND_TEXT as BACKEND_ICON_CLASS,
  type Backend,
} from '../../lib/backendAccent';

// Sentinel option that clears the model override back to the backend default
// (a combobox can't submit an empty value, so this is the explicit clear path).
const MODEL_DEFAULT_OPTION = '__default__';

type AgentsTabKey = 'definitions' | 'running';
const AGENTS_TAB_ORDER: AgentsTabKey[] = ['definitions', 'running'];

function isSystemAgent(agent: { source: string }): boolean {
  return agent.source === 'builtin' || agent.source === 'system';
}

export const AgentsPage: React.FC = () => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const [agentsTab, setAgentsTab] = useState<AgentsTabKey>('definitions');
  const [runningActiveCount, setRunningActiveCount] = useState<number | null>(null);
  const [eventBridgeConnected, setEventBridgeConnected] = useState(false);
  const [agents, setAgents] = useState<VibeAgentBrief[]>([]);
  const [defaultName, setDefaultName] = useState<string | null>(null);
  const [selected, setSelected] = useState<VibeAgentFull | null>(null);
  const [loading, setLoading] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [showGlobalPrompts, setShowGlobalPrompts] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [backendFilter, setBackendFilter] = useState<Backend | 'all'>('all');
  const [importing, setImporting] = useState<Backend | null>(null);
  // Mobile drill-down: a row tap opens the detail full-screen. The agent
  // auto-selected on mount stays in the list view until the user drills in.
  const [detailOpen, setDetailOpen] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.listVibeAgents({ includeDisabled: true });
      setAgents(result.agents);
      setDefaultName(result.default_agent_name);
      // Keep the currently-selected agent fresh after edits / refreshes.
      if (selected) {
        const fresh = result.agents.find((a) => a.name === selected.name);
        if (!fresh) setSelected(null);
      }
    } catch (err: any) {
      setError(err?.message ?? String(err));
    } finally {
      setLoading(false);
    }
  }, [api, selected]);

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-select the default agent on first load so the detail panel has
  // something to show — eliminates the empty "select an agent" state
  // that confused users on first visit.
  useEffect(() => {
    if (selected || agents.length === 0) return;
    const target = (defaultName && agents.find((a) => a.name === defaultName)) || agents[0];
    if (target) selectAgent(target.name);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [defaultName, agents]);

  // If the selection clears (agent deleted, or a refresh dropped it) drop the
  // mobile drill state too — otherwise the list stays max-lg:hidden with no
  // detail rendered, leaving the page blank with no way back.
  useEffect(() => {
    if (!selected) setDetailOpen(false);
  }, [selected]);

  const fetchRunningActiveCount = useCallback(async () => {
    try {
      const result = await api.getRunningAgents();
      if (result.ok && result.counts) {
        // Badge = the true live-session count (active + idle + orphan) from the
        // running-agents snapshot. Deliberately sourced here (not from the graph
        // tab's counts) so it stays independent of the graph's project/time/
        // visibility filters — a narrowed graph must not shrink the badge.
        setRunningActiveCount(result.counts.total ?? 0);
      } else {
        setRunningActiveCount(null);
      }
    } catch {
      setRunningActiveCount(null);
    }
  }, [api]);

  // Keep the badge fresh on every tab (including 运行) so it never depends on
  // the graph view's filters.
  useEffect(() => {
    void fetchRunningActiveCount();
  }, [fetchRunningActiveCount]);

  useEffect(() => {
    return api.connectWorkbenchEvents({
      onConnected: (data) => {
        if (data.source === 'controller') {
          setEventBridgeConnected(true);
          fetchRunningActiveCount();
        }
      },
      onEventBridgeStatus: ({ connected }) => {
        setEventBridgeConnected(connected);
        if (connected) fetchRunningActiveCount();
      },
      onError: () => setEventBridgeConnected(false),
      onRunsUpdated: () => fetchRunningActiveCount(),
      onTurnStart: () => fetchRunningActiveCount(),
      onTurnEnd: () => fetchRunningActiveCount(),
      onSessionStatus: () => fetchRunningActiveCount(),
    });
  }, [api, fetchRunningActiveCount]);

  useEffect(() => {
    // Reconcile the badge even while SSE is connected: process death / orphan /
    // reap is a sampled snapshot with no run/session SSE event, so a slow
    // liveness poll keeps the count fresh (30s connected, 8s disconnected),
    // mirroring the old running list.
    const intervalMs = eventBridgeConnected ? 30000 : 8000;
    let timer: number | undefined;
    let cancelled = false;
    let inFlight = false;
    let pendingWake = false;

    const tick = async () => {
      if (cancelled) return;
      if (document.visibilityState !== 'visible') {
        timer = window.setTimeout(tick, intervalMs);
        return;
      }
      if (inFlight) {
        pendingWake = true;
        return;
      }
      inFlight = true;
      window.clearTimeout(timer);
      try {
        await fetchRunningActiveCount();
      } finally {
        inFlight = false;
      }
      if (cancelled) return;
      if (pendingWake) {
        pendingWake = false;
        void tick();
        return;
      }
      timer = window.setTimeout(tick, intervalMs);
    };

    const refreshNow = () => {
      if (document.visibilityState === 'visible') void tick();
    };

    timer = window.setTimeout(tick, intervalMs);
    document.addEventListener('visibilitychange', refreshNow);
    window.addEventListener('focus', refreshNow);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', refreshNow);
      window.removeEventListener('focus', refreshNow);
    };
  }, [eventBridgeConnected, fetchRunningActiveCount]);

  const selectAgent = useCallback(
    async (name: string, openDetail = false) => {
      try {
        const result = await api.getVibeAgent(name);
        if (result.ok) {
          setSelected(result.agent);
          // Enter the mobile drill-down only once the detail has actually loaded —
          // never optimistically, or a failed fetch hides the list with no panel.
          if (openDetail) setDetailOpen(true);
        }
      } catch (err: any) {
        setError(err?.message ?? String(err));
      }
    },
    [api],
  );

  // Apply text search + backend filter; backend grouping is a layout
  // concern that operates on the filtered set.
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return agents.filter((agent) => {
      if (backendFilter !== 'all' && agent.backend !== backendFilter) return false;
      if (!q) return true;
      return (
        agent.name.toLowerCase().includes(q) ||
        (agent.description ?? '').toLowerCase().includes(q) ||
        (agent.model ?? '').toLowerCase().includes(q)
      );
    });
  }, [agents, search, backendFilter]);

  const grouped = useMemo(() => {
    const groups: Record<Backend, VibeAgentBrief[]> = { claude: [], opencode: [], codex: [] };
    for (const agent of filtered) {
      const key = (agent.backend as Backend) in groups ? (agent.backend as Backend) : null;
      if (key) groups[key].push(agent);
    }
    return groups;
  }, [filtered]);

  const onCreated = (agent: VibeAgentFull) => {
    refresh().then(() => setSelected(agent));
  };


  const updateField = async (patch: Partial<VibeAgentFull>) => {
    if (!selected) return;
    try {
      const result = await api.updateVibeAgent(selected.name, patch as any);
      if (result.ok) {
        setSelected(result.agent);
        refresh();
      }
    } catch (err: any) {
      setError(err?.message ?? String(err));
    }
  };

  // Promote the selected agent to the global default so plain "new chat"
  // / IM routing without an explicit agent lands here. Throws on failure
  // so the detail panel can surface a toast.
  const onSetDefault = async () => {
    if (!selected) return;
    await api.setDefaultVibeAgent(selected.name);
    setDefaultName(selected.name);
    refresh();
  };

  // After a rename (clone-then-delete) the list is stale: the old name lingers
  // and the new one is missing. Refresh and re-select the renamed agent.
  const onRenamed = (newName: string) => {
    refresh().then(() => selectAgent(newName));
  };

  const onDelete = async () => {
    if (!selected || isSystemAgent(selected)) return;
    const confirmed = window.confirm(t('agents.deleteConfirm', { name: selected.name }));
    if (!confirmed) return;
    try {
      const result = await api.removeVibeAgent(selected.name);
      if (result.ok) {
        setSelected(null);
        refresh();
      } else if (result.code === 'agent_in_use') {
        setError(t('agents.deleteInUse', { name: selected.name }));
      } else if (result.message) {
        setError(result.message);
      }
    } catch (err: any) {
      setError(err?.message ?? String(err));
    }
  };

  const onImport = async (from: Backend) => {
    setImporting(from);
    try {
      const result = await api.importVibeAgents({ from, all: true });
      if (result.ok) {
        // Backend returns newly imported agents under `imported` (see
        // vibe/api.py::import_vibe_agents); `created` was always undefined so
        // the toast reported 0 even on a successful import.
        const imported = result.imported?.length ?? 0;
        const skipped = result.skipped?.length ?? 0;
        if (imported === 0 && skipped === 0) {
          // Nothing on disk for this backend — say where we looked instead of a
          // confusing "imported 0" success toast.
          showToast(t('agents.importNoneFound', { backend: BACKEND_LABEL[from] }), 'warning');
        } else {
          showToast(t('agents.importSuccess', { imported, skipped }), 'success');
        }
        refresh();
      } else {
        showToast(
          t('agents.importFailed', { error: result.message || result.error || result.code || 'unknown' }),
          'error',
        );
      }
    } catch (err: any) {
      showToast(t('agents.importFailed', { error: err?.message ?? String(err) }), 'error');
    } finally {
      setImporting(null);
    }
  };

  const totalShown = filtered.length;
  const noMatches = totalShown === 0 && agents.length > 0;

  return (
    <div className="mx-auto flex w-full max-w-[1200px] flex-col gap-5 py-2">
      <CapabilityTabs />
      {/* Header — shared WorkbenchPageHeader (design.pen: 40px mint icon + title + subtitle). */}
      <WorkbenchPageHeader
        icon={<Bot className="size-5" />}
        title={t('agents.title')}
        subtitle={t('agents.subtitle', { count: agents.length })}
        actions={
          <Button type="button" variant="outline" size="xs" onClick={() => refresh()} disabled={loading}>
            <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} />
            {t('common.refresh')}
          </Button>
        }
      />

      {/* Sub-tab row: Definitions | Running */}
      <div className="flex items-center gap-0 overflow-x-auto border-b border-border">
        {AGENTS_TAB_ORDER.map((key) => {
          const active = agentsTab === key;
          return (
            <button
              key={key}
              type="button"
              onClick={() => setAgentsTab(key)}
              className={clsx(
                'flex shrink-0 items-center gap-2 whitespace-nowrap px-4 py-3 text-[13px] transition',
                active
                  ? 'border-b-2 border-violet font-bold text-violet'
                  : 'font-medium text-muted hover:text-foreground',
              )}
            >
              {key === 'definitions' ? (
                <Layers className={clsx('size-3.5', active ? 'text-violet' : 'text-muted')} />
              ) : (
                <Activity className={clsx('size-3.5', active ? 'text-violet' : 'text-muted')} />
              )}
              {t(`agents.tabs.${key}`)}
              {key === 'running' && (
                <span
                  className={clsx(
                    'rounded-full border px-1.5 py-0 font-mono text-[9px] font-bold',
                    active
                      ? 'border-violet/30 bg-violet/[0.10] text-violet'
                      : 'border-border-strong bg-foreground/[0.04] text-muted',
                  )}
                >
                  {runningActiveCount === null ? '—' : runningActiveCount}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* 运行 tab body — the run graph (replaces the old flat running list). */}
      {agentsTab === 'running' && <AgentGraphTab />}

      {/* Toolbar — design.pen Imduv: search + backend filter + spacer + Import + 新建 Agent */}
      <div className={clsx('flex flex-wrap items-center gap-2.5', agentsTab === 'running' ? 'hidden' : detailOpen && 'max-lg:hidden')}>
        <div className="flex h-9 w-full items-center gap-2 rounded-md border border-input bg-background px-3 transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring sm:w-[320px]">
          <Search className="size-3.5 shrink-0 text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('agents.searchPlaceholder')}
            className="flex-1 bg-transparent text-[12px] text-foreground outline-none placeholder:text-muted"
          />
        </div>
        <BackendFilter value={backendFilter} onChange={setBackendFilter} />
        <div className="flex-1" />
        <Button type="button" variant="outline" size="xs" onClick={() => setShowGlobalPrompts(true)}>
          <FileText className="size-3.5" />
          {t('globalPrompts.button')}
        </Button>
        <ImportMenu onImport={onImport} importing={importing} />
        <Button type="button" variant="brand" size="xs" onClick={() => setShowNew(true)}>
          <Plus />
          {t('agents.newAgent')}
        </Button>
      </div>

      {agentsTab === 'definitions' && error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/[0.06] px-3 py-2 text-[12px] text-destructive">
          {error}
        </div>
      )}

      {/* Body — list + detail. The detail column only renders when a row
          is selected; the empty "select an agent" placeholder used to
          dominate the right side of a fresh page. With auto-select on
          mount it's rarely needed; when it is empty we just collapse
          back to a single column.
          Hidden when Running tab is active; all hooks stay mounted. */}
      <div
        className={clsx(
          'grid gap-5',
          agentsTab === 'running' && 'hidden',
          // `minmax(0,1fr)` + `min-w-0` keep the list column shrinkable; bare
          // `1fr` would let a long agent row push the fixed detail card off-screen.
          selected ? 'grid-cols-1 lg:grid-cols-[minmax(0,1fr)_420px]' : 'grid-cols-1',
        )}
      >
        <div className={clsx('flex min-w-0 flex-col gap-4', detailOpen && 'max-lg:hidden')}>
          {BACKEND_ORDER.map((backend) => {
            const items = grouped[backend];
            if (!items || items.length === 0) return null;
            return (
              <div key={backend} className="flex flex-col gap-2">
                <div className="flex items-center gap-2 px-1">
                  <Bot className={clsx('size-3.5', BACKEND_ICON_CLASS[backend])} />
                  <span className={clsx('text-[13px] font-bold', BACKEND_ICON_CLASS[backend])}>
                    {BACKEND_LABEL[backend]}
                  </span>
                  <span className="font-mono text-[10px] text-muted">
                    {items.length} agents
                  </span>
                </div>
                <div className="flex flex-col gap-2">
                  {items.map((agent) => (
                    <AgentRow
                      key={agent.id}
                      agent={agent}
                      isSelected={selected?.name === agent.name}
                      isDefault={defaultName === agent.name}
                      onSelect={() => selectAgent(agent.name, true)}
                    />
                  ))}
                </div>
              </div>
            );
          })}

          {agents.length === 0 && !loading && (
            <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-surface px-6 py-16 text-center">
              <Bot className="size-8 text-muted" />
              <div className="text-[14px] font-semibold text-foreground">{t('agents.empty')}</div>
              <Button type="button" variant="brand" size="sm" onClick={() => setShowNew(true)}>
                <Plus />
                {t('agents.newAgent')}
              </Button>
            </div>
          )}

          {noMatches && (
            <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-10 text-center text-[12px] text-muted">
              {t('agents.noSearchMatch')}
            </div>
          )}
        </div>

        {selected && (
          <div className={clsx('self-start rounded-2xl border border-border-strong bg-surface p-5', !detailOpen && 'max-lg:hidden')}>
            <AgentDetailPanel
              agent={selected}
              isDefault={defaultName === selected.name}
              onChange={updateField}
              onSetDefault={onSetDefault}
              onRenamed={onRenamed}
              onDelete={onDelete}
              onClose={() => { setSelected(null); setDetailOpen(false); }}
            />
          </div>
        )}
      </div>

      <NewAgentDialog open={showNew} onClose={() => setShowNew(false)} onCreated={onCreated} />
      <GlobalPromptsDialog open={showGlobalPrompts} onClose={() => setShowGlobalPrompts(false)} />
    </div>
  );
};

// One row in the backend-grouped list. Hover state + click selects.
interface AgentRowProps {
  agent: VibeAgentBrief;
  isSelected: boolean;
  isDefault: boolean;
  onSelect: () => void;
}

const AgentRow: React.FC<AgentRowProps> = ({ agent, isSelected, isDefault, onSelect }) => {
  const { t } = useTranslation();
  const description = [agent.model, agent.reasoning_effort, agent.description].filter(Boolean).join(' · ');
  return (
    <button
      type="button"
      onClick={onSelect}
      className={clsx(
        'flex items-center gap-3 rounded-xl border px-4 py-3 text-left transition',
        isSelected
          ? 'border-mint/40 bg-mint-soft shadow-[0_0_18px_-10px_rgba(91,255,160,0.6)]'
          : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2',
      )}
    >
      <div className="flex flex-1 flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="text-[14px] font-semibold text-foreground">{agent.name}</span>
          {isDefault && <Badge variant="success" className="px-1.5 py-0 text-[9px] font-mono uppercase">DEFAULT</Badge>}
          {isSystemAgent(agent) && (
            <Badge variant="secondary" className="px-1.5 py-0 text-[9px] font-mono uppercase">SYSTEM</Badge>
          )}
        </div>
        {description && <div className="text-[11px] text-muted">{description}</div>}
      </div>
      <Badge variant={agent.enabled ? 'success' : 'secondary'} className="font-mono uppercase">
        <span className={clsx('size-1.5 rounded-full', agent.enabled ? 'bg-mint' : 'bg-muted')} />
        {agent.enabled ? t('agents.statusEnabled') : t('agents.statusDisabled')}
      </Badge>
    </button>
  );
};

interface BackendFilterProps {
  value: Backend | 'all';
  onChange: (next: Backend | 'all') => void;
}

// Compact Popover trigger that mirrors design.pen dMFRl — funnel icon +
// "Backend: All" label + chevron. Replaces the old hand-rolled checkbox.
const BackendFilter: React.FC<BackendFilterProps> = ({ value, onChange }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const label = value === 'all' ? t('agents.backendAll') : BACKEND_LABEL[value];
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-1.5 rounded-md border border-border-strong bg-surface px-3 py-2 text-[12px] font-medium text-foreground transition hover:bg-foreground/[0.04]"
        >
          <Funnel className="size-3 text-muted" />
          <span className="text-muted">{t('agents.backendFilter')}:</span>
          <span>{label}</span>
          <ChevronDown className="size-3 text-muted" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[180px] p-1">
        {(['all', ...BACKEND_ORDER] as const).map((key) => (
          <button
            key={key}
            type="button"
            onClick={() => {
              onChange(key);
              setOpen(false);
            }}
            className={clsx(
              'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] transition',
              value === key ? 'bg-cyan-soft text-cyan' : 'text-foreground hover:bg-foreground/[0.04]',
            )}
          >
            {key !== 'all' && <Bot className={clsx('size-3.5', BACKEND_ICON_CLASS[key])} />}
            <span>{key === 'all' ? t('agents.backendAll') : BACKEND_LABEL[key]}</span>
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
};

interface ImportMenuProps {
  onImport: (from: Backend) => void;
  importing: Backend | null;
}

// Outline Button that opens a popover with one entry per backend. The
// backend supports bulk import via `from=<backend>&all=true`, which
// surfaces every installed agent definition the user already has on
// disk for that backend.
const ImportMenu: React.FC<ImportMenuProps> = ({ onImport, importing }) => {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button type="button" variant="outline" size="xs" disabled={importing !== null}>
          {importing ? <Loader2 className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />}
          {t('agents.import')}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[200px] p-1">
        {BACKEND_ORDER.map((backend) => (
          <button
            key={backend}
            type="button"
            disabled={importing !== null}
            onClick={() => {
              onImport(backend);
              setOpen(false);
            }}
            className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12px] text-foreground transition hover:bg-foreground/[0.04] disabled:opacity-50"
          >
            <Bot className={clsx('size-3.5', BACKEND_ICON_CLASS[backend])} />
            <span>{t(`agents.importFrom${BACKEND_LABEL[backend]}` as const)}</span>
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
};

interface DetailProps {
  agent: VibeAgentFull;
  isDefault: boolean;
  onChange: (patch: Partial<VibeAgentFull>) => void;
  onSetDefault: () => Promise<void>;
  onRenamed: (newName: string) => void;
  onDelete: () => void;
  onClose: () => void;
}

// Mirrors design.pen s7QaWQ. Header (name + close X) → Enable card →
// Name → Backend (read-only) → Model (Combobox) → Reasoning effort →
// System Prompt (collapsible) → footer Run / Delete. Name is editable
// for user agents (rename = create-then-delete since backend keeps name
// as the immutable reference id). System agents lock the name.
const AgentDetailPanel: React.FC<DetailProps> = ({ agent, isDefault, onChange, onSetDefault, onRenamed, onDelete, onClose }) => {
  const { t } = useTranslation();
  const api = useApi();
  const { showToast } = useToast();
  const system = isSystemAgent(agent);
  const [name, setName] = useState(agent.name);
  const [renaming, setRenaming] = useState(false);
  const [settingDefault, setSettingDefault] = useState(false);
  const [description, setDescription] = useState(agent.description ?? '');
  const [model, setModel] = useState(agent.model ?? '');
  const [effort, setEffort] = useState(agent.reasoning_effort ?? 'medium');
  const [systemPrompt, setSystemPrompt] = useState(agent.system_prompt ?? '');
  const [systemPromptOpen, setSystemPromptOpen] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [modelCatalogs, setModelCatalogs] = useState<
    Record<
      string,
      {
        modelOptions: ComboboxOption[];
        reasoningOptions: Record<string, { value: string; label: string }[]>;
      }
    >
  >({});
  const [running, setRunning] = useState(false);

  const activeModelCatalog = modelCatalogs[agent.backend];
  const modelOptions = activeModelCatalog?.modelOptions ?? [];
  const reasoningOptions = activeModelCatalog?.reasoningOptions ?? {};

  useEffect(() => {
    setName(agent.name);
    setDescription(agent.description ?? '');
    setModel(agent.model ?? '');
    setEffort(agent.reasoning_effort ?? 'medium');
    setSystemPrompt(agent.system_prompt ?? '');
    setSystemPromptOpen(false);
    setEditorOpen(false);
  }, [agent.id]);

  // Load model catalog for the agent's backend so the Combobox can offer
  // suggestions. Keeps `allowCustomValue` so users can type a model the
  // backend doesn't know about yet (e.g. a freshly-released preview).
  useEffect(() => {
    return loadBackendModelsWithRefresh(
      api,
      agent.backend,
      ({ models, modelLabels, reasoningOptions: opts }) => {
        setModelCatalogs((current) => ({
          ...current,
          [agent.backend]: {
            modelOptions: models.map((m) => ({ value: m, label: modelOptionLabel(m, modelLabels) })),
            reasoningOptions: opts ?? {},
          },
        }));
      },
      () => {
        setModelCatalogs((current) => ({
          ...current,
          [agent.backend]: { modelOptions: [], reasoningOptions: {} },
        }));
      },
    );
  }, [agent.backend, api]);

  const systemPromptTokens = estimateTokens(systemPrompt);
  // Effort options follow the backend + selected model when the catalog provides them.
  const effortOptions = resolveEffortOptions(agent.backend, model, reasoningOptions);

  // Backend rejects PATCH /agents/<name> with a new name; the supported
  // way to rename is create-then-delete. We only let user agents do this
  // (system agents block both delete and name change).
  const commitRename = async () => {
    const trimmed = name.trim();
    if (!trimmed || trimmed === agent.name) {
      setName(agent.name);
      return;
    }
    if (system) {
      setName(agent.name);
      return;
    }
    setRenaming(true);
    try {
      // Clone with new name first so we never end up nameless on failure.
      await api.createVibeAgent({
        name: trimmed,
        backend: agent.backend,
        description: agent.description,
        model: agent.model,
        reasoning_effort: agent.reasoning_effort,
        system_prompt: agent.system_prompt,
        metadata: agent.metadata,
        enabled: agent.enabled,
      });
      const removeResult = await api.removeVibeAgent(agent.name);
      if (!removeResult.ok) {
        // Old name still has references — keep it but tell the user the
        // new one exists too.
        showToast(removeResult.message || t('agents.renameKeptOld'), 'warning');
      } else {
        showToast(t('agents.renameSuccess'), 'success');
      }
      // Carry the default over to the new name. removeVibeAgent() drops the
      // old row without moving default_agent_name, so renaming the default
      // agent would otherwise silently fall the default back to another agent.
      if (isDefault) {
        try {
          await api.setDefaultVibeAgent(trimmed);
        } catch (defErr: any) {
          showToast(defErr?.message ?? String(defErr), 'warning');
        }
      }
      // Refresh the list and re-select the renamed agent so the old name
      // drops out and the clone shows as the selected detail row.
      onRenamed(trimmed);
    } catch (err: any) {
      showToast(err?.message ?? String(err), 'error');
      setName(agent.name);
    } finally {
      setRenaming(false);
    }
  };

  const handleSetDefault = async () => {
    if (settingDefault) return;
    setSettingDefault(true);
    try {
      await onSetDefault();
      showToast(t('agents.detail.defaultSet', { name: agent.name }), 'success');
    } catch (err: any) {
      showToast(err?.message ?? String(err), 'error');
    } finally {
      setSettingDefault(false);
    }
  };

  return (
    <div className="flex flex-col gap-3.5">
      {/* Header row — design.pen j5dGQ8 without DEFAULT badge (now read-
          only via the list-row pill; the panel always shows the agent's
          current identity, not its "is-default" status). */}
      <div className="flex items-start gap-2.5">
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="truncate text-[16px] font-bold text-foreground">{agent.name}</div>
          <div className="truncate text-[10px] text-muted">
            Vibe Agent · {agent.backend} backend
          </div>
        </div>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onClose}
          aria-label={t('common.close')}
          className="size-6"
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
        </Button>
      </div>

      {/* Enable toggle — design.pen EWTY7 */}
      <div
        className={clsx(
          'flex items-center justify-between gap-3 rounded-[10px] border px-3.5 py-3',
          agent.enabled ? 'border-mint/40 bg-mint-soft' : 'border-border-strong bg-surface-2',
        )}
      >
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-[13px] font-bold text-foreground">{t('agents.detail.enabled')}</span>
          <span className="text-[11px] text-muted">{t('agents.detail.enabledHint')}</span>
        </div>
        <Switch
          checked={agent.enabled}
          onCheckedChange={(next) => onChange({ enabled: next })}
          label={t('agents.detail.enabled')}
        />
      </div>

      {/* Default routing — promotes this agent to the global default so a
          plain "new chat" (and IM routing without an explicit agent)
          lands here. Restores the set-default control dropped in the
          workbench rebuild; a disabled agent can't be the default. */}
      <div
        className={clsx(
          'flex items-center justify-between gap-3 rounded-[10px] border px-3.5 py-3',
          isDefault ? 'border-mint/40 bg-mint-soft' : 'border-border-strong bg-surface-2',
        )}
      >
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-[13px] font-bold text-foreground">{t('agents.detail.defaultTitle')}</span>
          <span className="text-[11px] text-muted">{t('agents.detail.defaultHint')}</span>
        </div>
        {isDefault ? (
          <Badge variant="success" className="font-mono uppercase">
            <Star className="size-3" />
            {t('agents.detail.defaultActive')}
          </Badge>
        ) : (
          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={handleSetDefault}
            disabled={settingDefault || !agent.enabled}
            title={!agent.enabled ? t('agents.detail.defaultNeedsEnabled') : undefined}
          >
            {settingDefault ? <Loader2 className="size-3 animate-spin" /> : <Star className="size-3" />}
            {t('agents.detail.setDefault')}
          </Button>
        )}
      </div>

      {/* Name — system agents are locked; user agents are editable via
          create-then-delete (no DB-level rename support). */}
      <Field label={t('agents.detail.name')}>
        <div className="flex items-center gap-2 rounded-lg border border-border-strong bg-surface-2 px-3 py-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
              if (e.key === 'Escape') setName(agent.name);
            }}
            disabled={system || renaming}
            title={system ? t('agents.detail.systemLocked') : undefined}
            className="flex-1 bg-transparent text-[13px] font-medium text-foreground outline-none disabled:cursor-not-allowed disabled:opacity-70"
          />
          {!system && <Pencil className="size-3 shrink-0 text-muted" />}
        </div>
      </Field>

      {/* Description — free-text summary of what the agent is for. Feeds the
          list-row subtitle (model · effort · description). Locked for system
          agents (same as the name); editable for user agents. */}
      <Field label={t('agents.detail.description')}>
        <Textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          onBlur={() => {
            if (!system && description !== (agent.description ?? '')) {
              onChange({ description: description.trim() || null });
            }
          }}
          disabled={system}
          title={system ? t('agents.detail.systemLocked') : undefined}
          rows={2}
          placeholder={t('agents.detail.descriptionPlaceholder')}
          className="text-[13px] disabled:cursor-not-allowed disabled:opacity-70"
        />
      </Field>

      {/* Backend (read-only) — design.pen JUopp. "creation-time only ·
          locked" hint sits inside the value chip on the right so users
          don't mistake it for a note about the field above (the name). */}
      <Field label={t('agents.detail.backend')}>
        <div className="flex items-center gap-2 rounded-lg border border-border bg-surface-3 px-3 py-2">
          <Bot className={clsx('size-3 shrink-0', BACKEND_ICON_CLASS[agent.backend as Backend] || 'text-muted')} />
          <span className={clsx('font-mono text-[12px] font-bold', BACKEND_ICON_CLASS[agent.backend as Backend] || 'text-foreground')}>
            {agent.backend}
          </span>
          <span className="text-[11px] text-muted">·</span>
          <span className="text-[11px] text-muted">{BACKEND_LABEL[agent.backend as Backend] || agent.backend} CLI</span>
          <span className="ml-auto font-mono text-[9px] text-muted">{t('agents.detail.backendLocked')}</span>
        </div>
      </Field>

      {/* Model — Combobox with chevron + searchable + custom values. The
          leading "backend default" option lets the user clear the override
          back to model: null (a combobox can't otherwise submit an empty
          value, so picking it is the only clear path). */}
      <Field label={t('agents.detail.model')}>
        <Combobox
          options={[{ value: MODEL_DEFAULT_OPTION, label: t('agents.detail.modelDefault') }, ...modelOptions]}
          value={model}
          onValueChange={(next) => {
            const value = next === MODEL_DEFAULT_OPTION ? '' : next;
            setModel(value);
            const patch: Partial<VibeAgentFull> = { model: value.trim() || null };
            // If the new model can't use the current effort, fall back to a
            // valid one and persist it in the same patch — otherwise the record
            // keeps an effort the model can't run (Codex P2).
            const opts = resolveEffortOptions(agent.backend, value, reasoningOptions);
            if (effort && !opts.includes(effort)) {
              const fallback = opts.includes('medium') ? 'medium' : opts[0];
              if (fallback) {
                setEffort(fallback);
                patch.reasoning_effort = fallback;
              }
            }
            onChange(patch);
          }}
          placeholder={t('agents.detail.modelPlaceholder')}
          emptyText={t('agents.detail.modelEmpty')}
          allowCustomValue
        />
      </Field>

      {/* Reasoning effort — design.pen LsjxT */}
      <Field label={t('agents.detail.effort')}>
        <div
          className="grid gap-0.5 rounded-lg border border-border-strong bg-surface-2 p-0.5"
          style={{ gridTemplateColumns: `repeat(${effortOptions.length}, minmax(0, 1fr))` }}
        >
          {effortOptions.map((opt) => {
            const active = effort === opt;
            return (
              <button
                key={opt}
                type="button"
                onClick={() => {
                  setEffort(opt);
                  onChange({ reasoning_effort: opt });
                }}
                className={clsx(
                  'truncate rounded-md px-1 py-1.5 text-[11px] capitalize transition',
                  active ? 'bg-mint-soft font-bold text-mint' : 'font-medium text-muted hover:text-foreground',
                )}
              >
                {opt}
              </button>
            );
          })}
        </div>
      </Field>

      {/* System prompt — design.pen y3mRv: collapsed by default. Token
          estimate (cheap heuristic, see lib/tokenEstimate) replaces the
          old character count so it's actually useful for budgeting. The
          textarea-level hint was deleted because the field label + the
          chevron row already tell the user what this is. */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setSystemPromptOpen((prev) => !prev)}
            className="flex flex-1 items-center gap-2.5 rounded-lg border border-border bg-foreground/[0.015] px-3 py-2.5 text-left transition hover:bg-foreground/[0.04]"
          >
            <ChevronRight
              className={clsx(
                'size-3 shrink-0 text-muted transition-transform',
                systemPromptOpen && 'rotate-90',
              )}
            />
            <span className="flex-1 text-[12px] font-semibold text-foreground">
              {t('agents.detail.systemPrompt')}
            </span>
            <span className="font-mono text-[10px] text-muted">
              {t('agents.detail.systemPromptCount', { count: systemPromptTokens })}
            </span>
          </button>
          {/* Expand into the full editor modal (large input + Markdown
              edit/preview) — the shared EditorDialog primitive. */}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-9 shrink-0 text-muted hover:text-foreground"
            onClick={() => setEditorOpen(true)}
            aria-label={t('agents.detail.systemPromptExpand')}
            title={t('agents.detail.systemPromptExpand')}
          >
            <Maximize2 className="size-3.5" />
          </Button>
        </div>
        {systemPromptOpen && (
          <Textarea
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            onBlur={() => {
              if (systemPrompt !== (agent.system_prompt ?? '')) {
                onChange({ system_prompt: systemPrompt.trim() || null });
              }
            }}
            rows={6}
            placeholder={t('agents.create.systemPromptPlaceholder')}
            className="text-[12px]"
          />
        )}
      </div>

      {/* Footer — Run on the left, Delete on the right. The Disable
          button was redundant with the top Enable toggle and was
          removed. */}
      <div className="flex items-center gap-2 pt-2">
        <Button
          type="button"
          variant="outline"
          size="xs"
          onClick={() => setRunning(true)}
          className="border-mint/40 bg-mint-soft text-mint hover:brightness-110"
        >
          <Play className="size-3" />
          {t('agents.detail.run')}
        </Button>
        <div className="flex-1" />
        {!system ? (
          <Button
            type="button"
            variant="destructive-soft"
            size="xs"
            onClick={onDelete}
          >
            <Trash2 className="size-3" />
            {t('common.delete')}
          </Button>
        ) : (
          <span className="text-[10px] text-muted">{t('agents.detail.systemLocked')}</span>
        )}
      </div>

      {running && <RunAgentDialog agent={agent} onClose={() => setRunning(false)} />}

      {/* Full-screen system-prompt editor — large input + Markdown preview.
          Opening from collapsed or expanded both jump straight here. */}
      <EditorDialog
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        title={t('agents.detail.systemPrompt')}
        description={t('agents.detail.systemPromptEditorHint')}
        value={systemPrompt}
        placeholder={t('agents.create.systemPromptPlaceholder')}
        footerHint={(draft) => t('agents.detail.systemPromptCount', { count: estimateTokens(draft) })}
        onSave={(next) => {
          setSystemPrompt(next);
          if (next !== (agent.system_prompt ?? '')) {
            onChange({ system_prompt: next.trim() || null });
          }
        }}
      />
    </div>
  );
};

const Field: React.FC<{ label: string; labelRight?: React.ReactNode; children: React.ReactNode }> = ({
  label,
  labelRight,
  children,
}) => (
  <div className="flex flex-col gap-1.5">
    <div className="flex items-center justify-between gap-2">
      <span className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-muted">{label}</span>
      {labelRight}
    </div>
    {children}
  </div>
);
