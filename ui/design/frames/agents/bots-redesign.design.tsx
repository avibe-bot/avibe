// The Agents page as the place where every Bot and Agent is configured (owner-approved 2026-11-02,
// shipped): Bots get their own section by face and name, with Add and Remove on the page; the Agents
// follow in one list; the panels draw only what applies. A Bot's Agent is never shown as an Agent:
// it is not in the Agents list, and comes back to it only when the Bot is removed. With every place
// for a custom Bot taken (D12) the page is where adding is explained: Add and "Set as Bot" stay in
// place, disabled, with the reason and the count.
// Drawn from the shipped view (`AgentsPageView`) and panel (`AgentDetailPanel`) beside the shipped
// sidebar at 1440×900, and on a phone.
import type { ReactNode } from 'react';

import { AgentDetailPanel } from '@/components/workbench/AgentsPage';
import { AgentsPageView, type AgentsPageViewProps } from '@/components/workbench/AgentsPageView';
import { RemoveBotDialog } from '@/components/workbench/bots/RemoveBotDialog';
import { WorkbenchSidebar } from '@/components/workbench/WorkbenchSidebar';
import type { VibeAgentBrief, VibeAgentFull } from '@/context/ApiContext';
import type { Bot } from '@/lib/bots';

import { frames } from '../../frame';
import { botLimitOf, demoBots, demoProjects, mainBot, owner, type WorkbenchFixtures } from '../../shell/fixtures';
import { fullTeam } from '../../shell/teamFixtures';
import { SidebarColumn } from '../../shell/SidebarColumn';
import { consoleContentClass, consolePageClass } from '../skills/fixtures';
import { agentBrief, demoAgents } from './fixtures';

// ── Data ─────────────────────────────────────────────────────────────────────

const asBot = (handle: string, displayName: string) => ({ handle, display_name: displayName, builtin: false });

/** The demo install, plus the Agents behind the user's two Bots and one Vibey Agent that could become one. */
const agents: VibeAgentBrief[] = [
  ...demoAgents.map((agent) => {
    if (agent.name === 'vibey' || agent.name === 'main') return { ...agent, model: 'gpt-6-astra', reasoning_effort: 'high' };
    if (agent.name === 'slack-triage') return { ...agent, enabled: false };
    return agent;
  }),
  agentBrief('cso', 'vibey', { model: 'gpt-6-astra', reasoning_effort: 'high', description: '依赖与权限的安全审计，每周汇报', bot: asBot('cso', '首席安全官') }),
  agentBrief('pm', 'vibey', { model: 'gpt-6-astra', reasoning_effort: 'medium', description: '需求池整理与优先级', bot: asBot('pm', '产品经理') }),
  agentBrief('researcher', 'vibey', { model: 'gpt-6-astra', reasoning_effort: 'medium', description: '查资料、比较方案，写成一页结论' }),
];

/** After the security lead is removed from Bots: its Agent is an ordinary Agent again (D1 keeps its
 *  conversation and memory, hidden until it is a Bot again). */
const afterRemoval = agents.map((agent) => (agent.name === 'cso' ? { ...agent, bot: null } : agent));
const builtinAgents = agents.filter((agent) => agent.source === 'builtin');

const PROMPT = '你负责依赖与权限的安全审计。每周一汇报一次：先列高危，再列建议。';

const full = (name: string, rows: VibeAgentBrief[] = agents): VibeAgentFull => {
  const agent = rows.find((each) => each.name === name);
  if (!agent) throw new Error(`no demo Agent ${name}`);
  return {
    ...agent,
    system_prompt: agent.source === 'builtin' ? null : PROMPT,
    metadata: agent.source === 'builtin' ? { builtin_default: true, lock_delete: true } : {},
    created_at: agent.updated_at,
  };
};

const efforts = (...values: string[]) => values.map((value) => ({ value, label: value }));

/** Each backend's model menu as a Direct-mode install reads it. */
const modelMenus: Record<string, unknown> = {
  ...Object.fromEntries(
    ['vibey', 'claude', 'codex', 'opencode'].map((backend) => [`/api/models/agents/${backend}/models`, { ok: true, agent: { backend, mode: 'direct' } }]),
  ),
  '/api/claude/models': {
    ok: true,
    models: ['claude-opus-5-5', 'claude-sonnet-5-5'],
    reasoning_options: { 'claude-opus-5-5': efforts('low', 'medium', 'high', 'max'), 'claude-sonnet-5-5': efforts('low', 'medium', 'high') },
  },
};

const team: WorkbenchFixtures = { projects: demoProjects, bots: demoBots, api: modelMenus };
const fresh: WorkbenchFixtures = { projects: demoProjects, bots: [mainBot], api: modelMenus };
const atLimit: WorkbenchFixtures = { projects: demoProjects, bots: fullTeam, api: modelMenus };
const removed: WorkbenchFixtures = { projects: demoProjects, bots: demoBots.filter((bot) => bot.handle !== 'cso'), api: modelMenus };
/** A member of a shared instance: Bots are the Owner's, so there are none here (D1). */
const member: WorkbenchFixtures = { projects: demoProjects, capabilities: { ...owner, is_instance_owner: false }, api: modelMenus };

const cso = demoBots.find((bot) => bot.handle === 'cso') as Bot;

// ── Layout ───────────────────────────────────────────────────────────────────

/** The desktop shell: the shipped sidebar column and the console page beside it. */
const desktop = (content: ReactNode) => () => (
  <div className="flex h-full">
    <div className="h-full w-62 shrink-0">
      <SidebarColumn>
        <WorkbenchSidebar />
      </SidebarColumn>
    </div>
    <div className={consolePageClass}>
      <div className={consoleContentClass}>{content}</div>
    </div>
  </div>
);

const phone = (content: ReactNode) => () => (
  <div className={consolePageClass}>
    <div className={consoleContentClass}>{content}</div>
  </div>
);

const noop = () => undefined;
const settled = () => Promise.resolve();

/** The page as `AgentsPage` hands it to its view: Bots, and the Agents that are not Bots. */
const page = ({
  rows = agents,
  bots = demoBots,
  ...props
}: Partial<AgentsPageViewProps> & { rows?: VibeAgentBrief[]; bots?: Bot[] | null }) => {
  const botLimit = bots ? botLimitOf(bots) : null;
  const botAgents = new Set((bots ?? []).map((bot) => bot.agent_name));
  const listed = rows.filter((agent) => !botAgents.has(agent.name) && !(bots === null && agent.bot));
  return (
    <AgentsPageView
      tabs={['definitions', 'running']}
      activeTab="definitions"
      runningCount={2}
      onSelectTab={noop}
      loading={false}
      onRefresh={noop}
      canEdit
      onGlobalPrompts={noop}
      search=""
      onSearchChange={noop}
      backendFilter="all"
      onBackendFilterChange={noop}
      bots={bots}
      botCount={bots?.length ?? 0}
      agentCount={listed.length}
      botLimit={botLimit}
      mainBot={bots?.find((bot) => bot.builtin) ?? null}
      onAddBot={noop}
      agents={listed}
      defaultAgentName="vibey"
      withoutModel={new Set()}
      selectedName={null}
      onSelect={noop}
      importing={null}
      onImport={noop}
      onNewAgent={noop}
      detailOpen={false}
      {...props}
    />
  );
};

const panel = (agent: VibeAgentFull, isDefault = false) => (
  <AgentDetailPanel
    agent={agent}
    isDefault={isDefault}
    canEdit
    canRun
    onChange={settled}
    onSetDefault={settled}
    onRename={settled}
    onDelete={noop}
    onClose={noop}
  />
);

// ── Frames ───────────────────────────────────────────────────────────────────

const designFrames = frames([
  {
    name: 'bot',
    title: '伙伴与 Agents · 选中一位伙伴',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/agents',
    fixtures: team,
    render: desktop(page({ selectedName: 'cso', detail: panel(full('cso')) })),
  },
  {
    name: 'agent',
    title: '伙伴与 Agents · 选中一个可设为伙伴的 Agent',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/agents',
    fixtures: team,
    render: desktop(page({ selectedName: 'researcher', detail: panel(full('researcher')) })),
  },
  {
    name: 'default-agent',
    title: '伙伴与 Agents · 默认 Agent（内置，只剩能改的）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/agents',
    fixtures: team,
    render: desktop(page({ selectedName: 'vibey', detail: panel(full('vibey'), true) })),
  },
  {
    name: 'at-limit',
    title: '伙伴与 Agents · 已满 5 位：「添加伙伴」和「设为伙伴」变灰，写明原因和数量',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/agents',
    fixtures: atLimit,
    render: desktop(page({ bots: fullTeam, selectedName: 'researcher', detail: panel(full('researcher')) })),
  },
  {
    name: 'remove-bot',
    title: '伙伴与 Agents · 移出伙伴前确认',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/agents',
    fixtures: team,
    render: desktop(
      <>
        {page({ selectedName: 'cso', detail: panel(full('cso')) })}
        <RemoveBotDialog bot={cso} open onOpenChange={noop} />
      </>,
    ),
  },
  {
    name: 'after-remove',
    title: '伙伴与 Agents · 移出之后：它的 Agent 回到 Agents',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/agents',
    fixtures: removed,
    render: desktop(
      page({
        rows: afterRemoval,
        bots: demoBots.filter((bot) => bot.handle !== 'cso'),
        selectedName: 'cso',
        detail: panel(full('cso', afterRemoval)),
      }),
    ),
  },
  {
    name: 'fresh',
    title: '伙伴与 Agents · 新安装：只有歪比，选中主伙伴',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/agents',
    fixtures: fresh,
    render: desktop(page({ rows: builtinAgents, bots: [mainBot], selectedName: 'main', detail: panel(full('main')) })),
  },
  {
    name: 'member',
    title: '伙伴与 Agents · 成员：没有伙伴区',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/agents',
    fixtures: member,
    render: desktop(page({ bots: null, selectedName: 'reviewer', detail: panel(full('reviewer')) })),
  },
  {
    name: 'phone',
    title: '伙伴与 Agents · 手机：列表',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/agents',
    fixtures: team,
    render: phone(page({})),
  },
  {
    name: 'phone-at-limit',
    title: '伙伴与 Agents · 手机 · 已满 5 位：「添加伙伴」变灰，写明原因和数量（加高到伙伴区结尾）',
    width: 390,
    height: 1000,
    status: 'shipped',
    route: '/agents',
    fixtures: atLimit,
    render: phone(page({ bots: fullTeam })),
  },
  {
    name: 'phone-agent-at-limit',
    title: '伙伴与 Agents · 手机 · 已满 5 位：「设为伙伴」变灰，写明原因',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/agents',
    fixtures: atLimit,
    render: phone(page({ bots: fullTeam, selectedName: 'researcher', detail: panel(full('researcher')), detailOpen: true })),
  },
  {
    name: 'phone-bot',
    title: '伙伴与 Agents · 手机：伙伴详情',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/agents',
    fixtures: team,
    render: phone(page({ selectedName: 'cso', detail: panel(full('cso')), detailOpen: true })),
  },
]);

export default designFrames;
