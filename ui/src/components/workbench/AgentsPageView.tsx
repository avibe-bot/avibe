// The Agents page's view (props only): the place where every Bot and Agent is configured.
// ui/design: agents/bots-redesign/bot. A Bots section first, by face and name, with Add; then the
// Agents, one list with backend icons in the composer's @ menu order (docs/plans/bots.md D3). Bots
// and Agents are two concepts: a Bot's Agent is never listed as an Agent (the container leaves it
// out), and returns to the Agents when its Bot is removed. `AgentsPage` owns the data and passes
// the selected Bot's or Agent's panel in as `detail`.
import { useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Activity,
  Bot as BotIcon,
  ChevronDown,
  FileText,
  Funnel,
  Layers,
  Loader2,
  Lock,
  Plus,
  RefreshCw,
  Search,
  Upload,
} from 'lucide-react';
import clsx from 'clsx';

import type { VibeAgentBrief } from '../../context/ApiContext';
import type { AgentsTabKey } from '../../lib/agentsViewMemory';
import { BACKEND_LABEL, BACKEND_ORDER, BACKEND_TEXT as BACKEND_ICON_CLASS, NATIVE_BACKEND_ORDER, isBackend, type Backend } from '../../lib/backendAccent';
import type { NativeCliBackend } from '../../lib/agentBackends';
import { botDisplayName, botRole, type Bot } from '../../lib/bots';
import { Badge } from '../ui/badge';
import { BotAvatar } from '../ui/bot-avatar';
import { Button } from '../ui/button';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';
import { BackendAvatar } from '../visual';
import { CapabilityTabs } from './CapabilityTabs';
import { WorkbenchPageHeader } from './WorkbenchPageHeader';

export interface AgentsPageViewProps {
  tabs: readonly AgentsTabKey[];
  activeTab: AgentsTabKey;
  /** Live Agents for the Running tab's count; `null` until the first count arrives. */
  runningCount: number | null;
  onSelectTab: (key: AgentsTabKey) => void;
  /** The Running tab's content. */
  running?: ReactNode;
  /** The organization-access summary, when the instance has one. */
  onboarding?: ReactNode;
  loading: boolean;
  onRefresh: () => void;
  /** Agent management: new, import, global prompts. Without it the page reads "read only". */
  canEdit: boolean;
  onGlobalPrompts: () => void;
  search: string;
  onSearchChange: (value: string) => void;
  backendFilter: Backend | 'all';
  onBackendFilterChange: (value: Backend | 'all') => void;
  /** The Bots the search and filter keep, main Bot first; `null` when the reader has no Bots (not the Owner). */
  bots: Bot[] | null;
  /** All Bots and all listed Agents, for the subtitle. */
  botCount: number;
  agentCount: number;
  onAddBot: () => void;
  /** The Agents the search and filter keep, without any Bot's Agent; the view orders them. */
  agents: VibeAgentBrief[];
  defaultAgentName: string | null;
  /** Agents the gateway cannot run for want of a model. */
  withoutModel: ReadonlySet<string>;
  /** The selected Agent's name; a Bot is selected by its Agent's name. */
  selectedName: string | null;
  onSelect: (agentName: string) => void;
  importing: NativeCliBackend | null;
  onImport: (from: NativeCliBackend) => void;
  onNewAgent: () => void;
  error?: string | null;
  /** The selected Bot's or Agent's panel. */
  detail?: ReactNode;
  /** Below lg the panel replaces the list (the mobile drill-down). */
  detailOpen: boolean;
}

const backendRank = (backend: string) => {
  const rank = isBackend(backend) ? BACKEND_ORDER.indexOf(backend) : -1;
  return rank === -1 ? BACKEND_ORDER.length : rank;
};

const isSystemAgent = (agent: { source: string }) => agent.source === 'builtin' || agent.source === 'system';

/** The Agents list order: by backend, the built-in Agent of each backend first, then by name. */
function agentsListOrder(agents: VibeAgentBrief[]): VibeAgentBrief[] {
  return [...agents].sort(
    (a, b) =>
      backendRank(a.backend) - backendRank(b.backend) ||
      Number(isSystemAgent(b)) - Number(isSystemAgent(a)) ||
      (a.name < b.name ? -1 : a.name > b.name ? 1 : 0),
  );
}

export const AgentsPageView: React.FC<AgentsPageViewProps> = ({
  tabs,
  activeTab,
  runningCount,
  onSelectTab,
  running,
  onboarding,
  loading,
  onRefresh,
  canEdit,
  onGlobalPrompts,
  search,
  onSearchChange,
  backendFilter,
  onBackendFilterChange,
  bots,
  botCount,
  agentCount,
  onAddBot,
  agents,
  defaultAgentName,
  withoutModel,
  selectedName,
  onSelect,
  importing,
  onImport,
  onNewAgent,
  error,
  detail,
  detailOpen,
}) => {
  const { t } = useTranslation();
  const searching = search.trim() !== '' || backendFilter !== 'all';
  const showBots = bots !== null && (bots.length > 0 || !searching);
  const listed = agentsListOrder(agents);
  const showAgents = listed.length > 0 || !searching;
  const definitions = activeTab === 'definitions';
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 py-2">
      <CapabilityTabs />
      <WorkbenchPageHeader
        icon={<BotIcon className="size-5" />}
        title={t('agents.title')}
        subtitle={
          bots !== null
            ? t('agents.subtitleWithBots', { bots: botCount, agents: agentCount })
            : t('agents.subtitle', { count: agentCount })
        }
        actions={
          <>
            {canEdit && (
              // On a phone it keeps its place as an icon: it is the only way to the global prompts.
              <Button type="button" variant="outline" size="xs" onClick={onGlobalPrompts} aria-label={t('globalPrompts.button')}>
                <FileText className="size-3.5" />
                <span className="max-sm:hidden">{t('globalPrompts.button')}</span>
              </Button>
            )}
            <Button type="button" variant="outline" size="xs" onClick={onRefresh}>
              <RefreshCw className={clsx('size-3.5', loading && 'animate-spin')} />
              {t('common.refresh')}
            </Button>
          </>
        }
      />

      {/* Local runtime diagnostics are intentionally unavailable remotely. */}
      <AgentsTabStrip tabs={tabs} active={activeTab} runningCount={runningCount} onSelect={onSelectTab} />

      {activeTab === 'running' && running}

      {definitions && onboarding && <div className={clsx(detailOpen && 'max-lg:hidden')}>{onboarding}</div>}

      {/* The Definitions tab stays mounted while Running shows, so the open panel keeps its edits. */}
      <div className={clsx('flex flex-col gap-5', !definitions && 'hidden')}>
        <div className={clsx('flex flex-wrap items-center gap-2.5', detailOpen && 'max-lg:hidden')}>
          <div className="flex h-9 w-full items-center gap-2 rounded-md border border-input bg-background px-3 transition-colors focus-within:border-ring focus-within:ring-2 focus-within:ring-ring sm:w-80">
            <Search className="size-3.5 shrink-0 text-muted" />
            <input
              value={search}
              onChange={(event) => onSearchChange(event.target.value)}
              placeholder={bots !== null ? t('agents.searchPlaceholderWithBots') : t('agents.searchPlaceholder')}
              className="min-w-0 flex-1 bg-transparent text-xs text-foreground outline-none placeholder:text-muted"
            />
          </div>
          <BackendFilter value={backendFilter} onChange={onBackendFilterChange} />
        </div>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/6 px-3 py-2 text-xs text-destructive-ink">
            {error}
          </div>
        )}

        <div className="flex items-start gap-5">
          <div className={clsx('flex min-w-0 flex-1 flex-col gap-7', detailOpen && 'max-lg:hidden')}>
            {showBots && (
              <BotsSection
                bots={bots ?? []}
                withoutModel={withoutModel}
                selectedName={selectedName}
                onSelect={onSelect}
                onAdd={canEdit ? onAddBot : undefined}
              />
            )}
            {showAgents && (
              <section className="flex flex-col gap-2.5">
                <SectionHeading
                  title={t('agents.title')}
                  count={listed.length}
                  actions={
                    canEdit ? (
                      <>
                        <ImportMenu onImport={onImport} importing={importing} />
                        <Button type="button" variant="brand" size="xs" onClick={onNewAgent}>
                          <Plus />
                          {t('agents.newAgent')}
                        </Button>
                      </>
                    ) : (
                      <Badge variant="secondary" title={t('agents.remoteReadOnlyHint')}>
                        <Lock className="size-3" />
                        {t('agents.remoteReadOnly')}
                      </Badge>
                    )
                  }
                />
                <div className="flex flex-col gap-1.5">
                  {listed.map((agent) => (
                    <AgentListRow
                      key={agent.id}
                      agent={agent}
                      isDefault={agent.name === defaultAgentName}
                      modelMissing={agent.enabled && withoutModel.has(agent.name)}
                      selected={selectedName === agent.name}
                      onSelect={() => onSelect(agent.name)}
                    />
                  ))}
                </div>
                {agentCount === 0 && !loading && !searching && (
                  <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-border bg-surface px-6 py-12 text-center">
                    <BotIcon className="size-8 text-muted" />
                    <div className="text-sm font-semibold text-foreground">{t('agents.empty')}</div>
                  </div>
                )}
              </section>
            )}
            {!showBots && !showAgents && (
              <div className="rounded-xl border border-dashed border-border bg-surface px-6 py-10 text-center text-xs text-muted">
                {bots !== null ? t('agents.noSearchMatchWithBots') : t('agents.noSearchMatch')}
              </div>
            )}
          </div>
          {detail && (
            <aside
              className={clsx(
                'w-full shrink-0 self-start rounded-2xl border border-border-strong bg-surface p-5 lg:w-105',
                !detailOpen && 'max-lg:hidden',
              )}
            >
              {detail}
            </aside>
          )}
        </div>
      </div>
    </div>
  );
};

function SectionHeading({ title, count, actions }: { title: string; count: number; actions?: ReactNode }) {
  return (
    <div className="flex min-h-8 items-center gap-2 px-1">
      <h2 className="text-sm font-bold text-foreground">{title}</h2>
      <span className="font-mono text-xs text-muted">{count}</span>
      <div className="flex-1" />
      {actions}
    </div>
  );
}

/** The main Bot first, then the user's Bots in their strip order, then Add. */
function BotsSection({
  bots,
  withoutModel,
  selectedName,
  onSelect,
  onAdd,
}: {
  bots: Bot[];
  /** Agents the gateway cannot run for want of a model: a Bot whose Agent is one says so. */
  withoutModel: ReadonlySet<string>;
  selectedName: string | null;
  onSelect: (agentName: string) => void;
  /** Absent without the right to add one. */
  onAdd?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <section className="flex flex-col gap-2.5">
      <SectionHeading title={t('bots.sectionLabel')} count={bots.length} />
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {bots.map((bot) => (
          <BotCard
            key={bot.handle}
            bot={bot}
            modelMissing={withoutModel.has(bot.agent_name)}
            selected={selectedName === bot.agent_name}
            onSelect={() => onSelect(bot.agent_name)}
          />
        ))}
        {onAdd && (
          <button
            type="button"
            onClick={onAdd}
            className="flex items-center gap-3 rounded-xl border border-dashed border-border-strong px-3.5 py-3 text-left text-muted transition hover:border-mint/50 hover:text-mint-ink"
          >
            <span className="grid size-10 shrink-0 place-items-center rounded-full border border-dashed border-current">
              <Plus className="size-4" />
            </span>
            <span className="text-sm font-semibold">{t('bots.addLabel')}</span>
          </button>
        )}
      </div>
    </section>
  );
}

const rowClass = (selected: boolean) =>
  clsx(
    'flex items-center gap-3 rounded-xl border px-3.5 text-left transition',
    selected
      ? 'border-mint/40 bg-mint-soft shadow-glow-sm-mint'
      : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2',
  );

function BotCard({
  bot,
  modelMissing,
  selected,
  onSelect,
}: {
  bot: Bot;
  /** Its next turn fails: the gateway has no model selected for its Agent. */
  modelMissing: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  const { t } = useTranslation();
  const name = botDisplayName(bot, t('bots.mainDefaultName'));
  const role = botRole(bot, t('bots.mainRole'));
  return (
    <button type="button" aria-current={selected ? 'true' : undefined} onClick={onSelect} className={clsx(rowClass(selected), 'py-3')}>
      <BotAvatar avatar={bot.avatar} name={name} size={40} />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-sm font-semibold text-foreground">{name}</span>
          {bot.builtin && (
            <Badge variant="success" className="shrink-0 px-1.5 py-0">
              {t('bots.mainBadge')}
            </Badge>
          )}
        </span>
        {role && <span className="truncate text-xs text-muted">{role}</span>}
      </span>
      {modelMissing && (
        <Badge variant="warning" className="shrink-0">
          {t('settings.models.gateway.agentIssues.modelMissing')}
        </Badge>
      )}
    </button>
  );
}

const modelSummary = (agent: VibeAgentBrief) => [agent.model, agent.reasoning_effort].filter(Boolean).join(' · ');

function AgentListRow({
  agent,
  isDefault,
  modelMissing,
  selected,
  onSelect,
}: {
  agent: VibeAgentBrief;
  isDefault: boolean;
  /** Enabled, but its next turn fails: the gateway has no model selected for it. */
  modelMissing: boolean;
  selected: boolean;
  onSelect: () => void;
}) {
  const { t } = useTranslation();
  const summary = modelSummary(agent);
  return (
    <button type="button" aria-current={selected ? 'true' : undefined} onClick={onSelect} className={clsx(rowClass(selected), 'py-2.5')}>
      <AgentGlyph backend={agent.backend} muted={!agent.enabled} />
      <span className={clsx('flex min-w-0 flex-1 flex-col gap-0.5', !agent.enabled && 'opacity-60')}>
        <span className="flex items-center gap-1.5">
          <span className="truncate text-sm font-semibold text-foreground">{agent.name}</span>
          {isDefault && (
            <Badge variant="success" className="shrink-0 px-1.5 py-0">
              {t('common.default')}
            </Badge>
          )}
        </span>
        {agent.description && <span className="truncate text-xs text-muted">{agent.description}</span>}
      </span>
      {summary && agent.enabled && <span className="shrink-0 truncate font-mono text-xs text-muted max-sm:hidden">{summary}</span>}
      {modelMissing && (
        <Badge variant="warning" className="shrink-0">
          {t('settings.models.gateway.agentIssues.modelMissing')}
        </Badge>
      )}
      {!agent.enabled && (
        <Badge variant="secondary" className="shrink-0">
          {t('agents.statusDisabled')}
        </Badge>
      )}
    </button>
  );
}

/** An Agent's face: its backend's icon on a tile, as the composer's @ menu draws Agents. */
export function AgentGlyph({ backend, muted = false, size = 'md' }: { backend: string; muted?: boolean; size?: 'md' | 'lg' }) {
  return (
    <span
      className={clsx(
        'grid shrink-0 place-items-center border border-border bg-surface-2',
        size === 'lg' ? 'size-12 rounded-xl' : 'size-8 rounded-lg',
        muted && 'opacity-60',
      )}
    >
      <BackendAvatar
        backend={backend}
        className={clsx(size === 'lg' ? 'size-6' : 'size-4', isBackend(backend) ? BACKEND_ICON_CLASS[backend] : 'text-muted')}
      />
    </span>
  );
}

interface AgentsTabStripProps {
  tabs: readonly AgentsTabKey[];
  active: AgentsTabKey;
  /** Active running Agents; `null` until the first count arrives. */
  runningCount: number | null;
  onSelect: (key: AgentsTabKey) => void;
}

// The Definitions / Running tabs. A view (props only), so a design frame can draw it.
const AgentsTabStrip: React.FC<AgentsTabStripProps> = ({ tabs, active: activeTab, runningCount, onSelect }) => {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-0 overflow-x-auto border-b border-border">
      {tabs.map((key) => {
        const active = activeTab === key;
        return (
          <button
            key={key}
            type="button"
            onClick={() => onSelect(key)}
            className={clsx(
              'flex shrink-0 items-center gap-2 whitespace-nowrap px-4 py-3 text-[13px] transition',
              active
                ? 'border-b-2 border-violet font-bold text-violet-ink'
                : 'font-medium text-muted hover:text-foreground',
            )}
          >
            {key === 'definitions' ? (
              <Layers className={clsx('size-3.5', active ? 'text-violet-ink' : 'text-muted')} />
            ) : (
              <Activity className={clsx('size-3.5', active ? 'text-violet-ink' : 'text-muted')} />
            )}
            {t(`agents.tabs.${key}`)}
            {key === 'running' && runningCount !== null && (
              <span
                className={clsx(
                  'rounded-full border px-1.5 py-0 font-mono text-[9px] font-bold',
                  active
                    ? 'border-violet/30 bg-violet/[0.10] text-violet-ink'
                    : 'border-border-strong bg-foreground/[0.04] text-muted',
                )}
              >
                {runningCount}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
};

interface BackendFilterProps {
  value: Backend | 'all';
  onChange: (next: Backend | 'all') => void;
}

// Compact Popover trigger: funnel icon +
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
          <span className="text-muted">{t('agents.backendFilterLabel')}</span>
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
              value === key ? 'bg-cyan-soft text-cyan-ink' : 'text-foreground hover:bg-foreground/[0.04]',
            )}
          >
            {key !== 'all' && <BackendAvatar backend={key} className={clsx('size-3.5', BACKEND_ICON_CLASS[key])} />}
            <span>{key === 'all' ? t('agents.backendAll') : BACKEND_LABEL[key]}</span>
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
};

interface ImportMenuProps {
  onImport: (from: NativeCliBackend) => void;
  importing: NativeCliBackend | null;
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
        {NATIVE_BACKEND_ORDER.map((backend) => (
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
            <BackendAvatar backend={backend} className={clsx('size-3.5', BACKEND_ICON_CLASS[backend])} />
            <span>{t('agents.importFrom', { backend: BACKEND_LABEL[backend] })}</span>
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
};
