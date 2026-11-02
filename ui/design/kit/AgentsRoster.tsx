// The Agents page as the place where every Bot and Agent is configured (proposal, 2026-11-02):
// a Bots section first, by face and name, with its own Add; then the Agents, one flat list with
// backend icons, the order the composer's @ menu uses (docs/plans/bots.md D3).
// Bots and Agents are two concepts (owner rule, 2026-11-02): the Agent behind a Bot, the main Bot's
// `main` included, is an implementation detail and is never shown or offered as an Agent. A removed
// Bot's Agent is an ordinary Agent again and returns to the Agents list.
// The selected Bot or Agent opens beside the list (below lg, in place of it).
import type { ReactNode } from 'react';
import { Bot as BotIcon, FileText, Plus, RefreshCw, Search } from 'lucide-react';
import clsx from 'clsx';
import { useTranslation } from 'react-i18next';

import { AgentsTabStrip, BackendFilter, ImportMenu } from '@/components/workbench/AgentsPage';
import { CapabilityTabs } from '@/components/workbench/CapabilityTabs';
import { WorkbenchPageHeader } from '@/components/workbench/WorkbenchPageHeader';
import { Badge } from '@/components/ui/badge';
import { BotAvatar } from '@/components/ui/bot-avatar';
import { Button } from '@/components/ui/button';
import { BackendAvatar } from '@/components/visual';
import type { VibeAgentBrief } from '@/context/ApiContext';
import { useBots } from '@/context/BotsContext';
import { BACKEND_ORDER, BACKEND_TEXT, isBackend } from '@/lib/backendAccent';
import type { Bot } from '@/lib/bots';

import { useBotRole } from './botRole';
import { useDraft } from './draft';

export type RosterSelection = { kind: 'bot'; handle: string } | { kind: 'agent'; name: string } | null;

export interface AgentsRosterProps {
  /** Every Agent `/api/agents` lists; a Bot's Agent is left out (the Bot stands for it). */
  agents: VibeAgentBrief[];
  defaultAgentName: string | null;
  selected?: RosterSelection;
  /** The selected Bot's or Agent's panel (`BotDetail`, `AgentDetail`). */
  detail?: ReactNode;
  /** Below lg the panel replaces the list, as the shipped page drills down. */
  detailOpen?: boolean;
  /** Agents the gateway cannot run for want of a model. */
  withoutModel?: string[];
  /** Agent management (new, import, global prompts). */
  canEdit?: boolean;
}

const backendRank = (backend: string) => {
  const rank = isBackend(backend) ? BACKEND_ORDER.indexOf(backend) : -1;
  return rank === -1 ? BACKEND_ORDER.length : rank;
};

const isSystem = (agent: VibeAgentBrief) => agent.source === 'builtin' || agent.source === 'system';

/** The Agents list: no Bot's Agent; by backend, the built-in Agent of each backend first, then by name. */
function rosterAgents(agents: VibeAgentBrief[]): VibeAgentBrief[] {
  return agents
    .filter((agent) => !agent.bot && !agent.archived)
    .sort(
      (a, b) =>
        backendRank(a.backend) - backendRank(b.backend) ||
        Number(isSystem(b)) - Number(isSystem(a)) ||
        a.name.localeCompare(b.name),
    );
}

export function AgentsRoster({
  agents,
  defaultAgentName,
  selected = null,
  detail,
  detailOpen = false,
  withoutModel = [],
  canEdit = true,
}: AgentsRosterProps) {
  const { t } = useTranslation();
  const draft = useDraft();
  const { enabled: botsEnabled, bots } = useBots();
  const listed = rosterAgents(agents);
  const subtitle = botsEnabled
    ? draft(`${bots.length} 个伙伴 · ${listed.length} 个 Agent`, `${bots.length} Bots · ${listed.length} Agents`)
    : draft(`${listed.length} 个 Agent`, `${listed.length} Agents`);
  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 py-2">
      <CapabilityTabs />
      <WorkbenchPageHeader
        icon={<BotIcon className="size-5" />}
        title={t('agents.title')}
        subtitle={subtitle}
        actions={
          <>
            {canEdit && (
              <Button type="button" variant="outline" size="xs" className="max-sm:hidden">
                <FileText className="size-3.5" />
                {t('globalPrompts.button')}
              </Button>
            )}
            <Button type="button" variant="outline" size="xs">
              <RefreshCw className="size-3.5" />
              {t('common.refresh')}
            </Button>
          </>
        }
      />
      <AgentsTabStrip tabs={['definitions', 'running']} active="definitions" runningCount={2} onSelect={() => undefined} />

      <div className={clsx('flex flex-wrap items-center gap-2.5', detailOpen && 'max-lg:hidden')}>
        <div className="flex h-9 w-full items-center gap-2 rounded-md border border-input bg-background px-3 sm:w-80">
          <Search className="size-3.5 shrink-0 text-muted" />
          <span className="flex-1 truncate text-xs text-muted">
            {botsEnabled ? draft('搜索伙伴或 Agent…', 'Search Bots and Agents…') : t('agents.searchPlaceholder')}
          </span>
        </div>
        <BackendFilter value="all" onChange={() => undefined} />
      </div>

      <div className="flex items-start gap-5">
        <div className={clsx('flex min-w-0 flex-1 flex-col gap-7', detailOpen && 'max-lg:hidden')}>
          {botsEnabled && <BotsSection selected={selected} />}
          <section className="flex flex-col gap-2.5">
            <SectionHeading
              title="Agents"
              count={listed.length}
              actions={
                canEdit ? (
                  <>
                    <ImportMenu onImport={() => undefined} importing={null} />
                    <Button type="button" variant="brand" size="xs">
                      <Plus />
                      {t('agents.newAgent')}
                    </Button>
                  </>
                ) : null
              }
            />
            <div className="flex flex-col gap-1.5">
              {listed.map((agent) => (
                <AgentRosterRow
                  key={agent.id}
                  agent={agent}
                  isDefault={agent.name === defaultAgentName}
                  modelMissing={agent.enabled && withoutModel.includes(agent.name)}
                  selected={selected?.kind === 'agent' && selected.name === agent.name}
                />
              ))}
            </div>
          </section>
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
  );
}

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
function BotsSection({ selected }: { selected: RosterSelection }) {
  const draft = useDraft();
  const { bots, displayName } = useBots();
  return (
    <section className="flex flex-col gap-2.5">
      <SectionHeading title={draft('伙伴', 'Bots')} count={bots.length} />
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {bots.map((bot) => (
          <BotRosterCard
            key={bot.handle}
            bot={bot}
            name={displayName(bot)}
            selected={selected?.kind === 'bot' && selected.handle === bot.handle}
          />
        ))}
        <button
          type="button"
          className="flex items-center gap-3 rounded-xl border border-dashed border-border-strong px-3.5 py-3 text-left text-muted transition hover:border-mint/50 hover:text-mint-ink"
        >
          <span className="grid size-10 shrink-0 place-items-center rounded-full border border-dashed border-current">
            <Plus className="size-4" />
          </span>
          <span className="text-sm font-semibold">{draft('添加伙伴', 'Add a Bot')}</span>
        </button>
      </div>
    </section>
  );
}

function BotRosterCard({ bot, name, selected }: { bot: Bot; name: string; selected: boolean }) {
  const draft = useDraft();
  const role = useBotRole()(bot);
  return (
    <button
      type="button"
      aria-current={selected ? 'true' : undefined}
      className={clsx(
        'flex items-center gap-3 rounded-xl border px-3.5 py-3 text-left transition',
        selected
          ? 'border-mint/40 bg-mint-soft shadow-glow-sm-mint'
          : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2',
      )}
    >
      <BotAvatar avatar={bot.avatar} name={name} size={40} />
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="flex items-center gap-1.5">
          <span className="truncate text-sm font-semibold text-foreground">{name}</span>
          {bot.builtin && (
            <Badge variant="success" className="shrink-0 px-1.5 py-0">
              {draft('主伙伴', 'Main')}
            </Badge>
          )}
        </span>
        {role && <span className="truncate text-xs text-muted">{role}</span>}
      </span>
    </button>
  );
}

const modelSummary = (agent: VibeAgentBrief) => [agent.model, agent.reasoning_effort].filter(Boolean).join(' · ');

function AgentRosterRow({
  agent,
  isDefault,
  modelMissing,
  selected,
}: {
  agent: VibeAgentBrief;
  isDefault: boolean;
  modelMissing: boolean;
  selected: boolean;
}) {
  const { t } = useTranslation();
  const draft = useDraft();
  const summary = modelSummary(agent);
  return (
    <button
      type="button"
      aria-current={selected ? 'true' : undefined}
      className={clsx(
        'flex items-center gap-3 rounded-xl border px-3.5 py-2.5 text-left transition',
        selected
          ? 'border-mint/40 bg-mint-soft shadow-glow-sm-mint'
          : 'border-border bg-surface hover:border-border-strong hover:bg-surface-2',
      )}
    >
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
          {draft('已停用', 'Off')}
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
        className={clsx(size === 'lg' ? 'size-6' : 'size-4', isBackend(backend) ? BACKEND_TEXT[backend] : 'text-muted')}
      />
    </span>
  );
}
