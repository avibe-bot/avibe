// Proposal (owner ask, 2026-11-02): the Agents page is where every Bot and Agent is configured.
// Bots get their own section by face and name, with Add and Remove on the page; the Agents follow in
// one list; the panels drop every control that cannot apply. A Bot's Agent is never shown as an Agent
// (owner rule): it is not in the Agents list, and comes back to it only when the Bot is removed.
// Drawn beside the shipped sidebar at 1440×900, and on a phone. Kit: `kit/AgentsRoster.tsx`,
// `kit/AgentsRosterDetail.tsx`.
import type { ReactNode } from 'react';

import { WorkbenchSidebar } from '@/components/workbench/WorkbenchSidebar';
import type { VibeAgentBrief } from '@/context/ApiContext';

import { frames } from '../../frame';
import { AgentsRoster, type AgentsRosterProps } from '../../kit/AgentsRoster';
import { AgentDetail, BotDetail, RemoveBotConfirm, type DetailAgent } from '../../kit/AgentsRosterDetail';
import { demoBots, demoProjects, mainBot, owner, type WorkbenchFixtures } from '../../shell/fixtures';
import { SidebarColumn } from '../../shell/SidebarColumn';
import { consoleContentClass, consolePageClass } from '../skills/fixtures';
import { agentBrief, demoAgents } from './fixtures';

// ── Data ─────────────────────────────────────────────────────────────────────

const asBot = (handle: string, displayName: string) => ({ handle, display_name: displayName, builtin: false });

/** The demo install, plus the Agents behind the user's two Bots and one Vibey Agent that could become one. */
const agents: VibeAgentBrief[] = [
  ...demoAgents.map((agent) => {
    if (agent.name === 'vibey') return { ...agent, model: 'gpt-6-astra', reasoning_effort: 'high' };
    if (agent.name === 'main') return { ...agent, model: 'gpt-6-astra', reasoning_effort: 'high' };
    if (agent.name === 'slack-triage') return { ...agent, enabled: false };
    return agent;
  }),
  agentBrief('cso', 'vibey', { model: 'gpt-6-astra', reasoning_effort: 'high', description: '依赖与权限的安全审计，每周汇报', bot: asBot('cso', '首席安全官') }),
  agentBrief('pm', 'vibey', { model: 'gpt-6-astra', reasoning_effort: 'medium', description: '需求池整理与优先级', bot: asBot('pm', '产品经理') }),
  agentBrief('researcher', 'vibey', { model: 'gpt-6-astra', reasoning_effort: 'medium', description: '查资料、比较方案，写成一页结论' }),
];

/** A fresh install: the main Bot, and each backend's built-in Agent. */
const builtinAgents = agents.filter((agent) => agent.source === 'builtin');

const EFFORTS: Record<string, string[]> = {
  vibey: ['low', 'medium', 'high', 'xhigh'],
  claude: ['low', 'medium', 'high', 'max'],
  codex: ['low', 'medium', 'high', 'xhigh'],
  opencode: ['low', 'medium', 'high'],
};

const detail = (name: string, promptTokens = 0): DetailAgent => {
  const agent = agents.find((each) => each.name === name);
  if (!agent) throw new Error(`no demo Agent ${name}`);
  return { ...agent, promptTokens, efforts: EFFORTS[agent.backend] ?? [] };
};

/** After the security lead is removed from Bots: its Agent is an ordinary Agent again (D1 keeps its
 *  conversation and memory, hidden until it is a Bot again). */
const afterRemoval = agents.map((agent) => (agent.name === 'cso' ? { ...agent, bot: null } : agent));
const csoAgent = (): DetailAgent => ({ ...detail('cso', 640), bot: null });

const team: WorkbenchFixtures = { projects: demoProjects, bots: demoBots };
const fresh: WorkbenchFixtures = { projects: demoProjects, bots: [mainBot] };
/** A member of a shared instance: Bots are the Owner's, so there are none here (D1). */
const member: WorkbenchFixtures = { projects: demoProjects, capabilities: { ...owner, is_instance_owner: false } };

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

const roster = (props: Partial<AgentsRosterProps>) => (
  <AgentsRoster agents={agents} defaultAgentName="vibey" withoutModel={[]} {...props} />
);

const csoPanel = <BotDetail handle="cso" agent={detail('cso', 640)} />;

// ── Frames ───────────────────────────────────────────────────────────────────

const designFrames = frames([
  {
    name: 'bot',
    title: '伙伴与 Agents · 选中一位伙伴',
    width: 1440,
    height: 900,
    status: 'proposed',
    route: '/agents',
    fixtures: team,
    render: desktop(roster({ selected: { kind: 'bot', handle: 'cso' }, detail: csoPanel })),
  },
  {
    name: 'agent',
    title: '伙伴与 Agents · 选中一个可设为伙伴的 Agent',
    width: 1440,
    height: 900,
    status: 'proposed',
    route: '/agents',
    fixtures: team,
    render: desktop(
      roster({
        selected: { kind: 'agent', name: 'researcher' },
        detail: <AgentDetail agent={detail('researcher', 180)} isDefault={false} />,
      }),
    ),
  },
  {
    name: 'default-agent',
    title: '伙伴与 Agents · 默认 Agent（内置，只剩能改的）',
    width: 1440,
    height: 900,
    status: 'proposed',
    route: '/agents',
    fixtures: team,
    render: desktop(
      roster({
        selected: { kind: 'agent', name: 'vibey' },
        detail: <AgentDetail agent={detail('vibey')} isDefault />,
      }),
    ),
  },
  {
    name: 'remove-bot',
    title: '伙伴与 Agents · 移出伙伴前确认',
    width: 1440,
    height: 900,
    status: 'proposed',
    route: '/agents',
    fixtures: team,
    render: desktop(
      <>
        {roster({ selected: { kind: 'bot', handle: 'cso' }, detail: csoPanel })}
        <RemoveBotConfirm handle="cso" />
      </>,
    ),
  },
  {
    name: 'after-remove',
    title: '伙伴与 Agents · 移出之后：它的 Agent 回到 Agents',
    width: 1440,
    height: 900,
    status: 'proposed',
    route: '/agents',
    fixtures: { projects: demoProjects, bots: demoBots.filter((bot) => bot.handle !== 'cso') },
    render: desktop(
      <AgentsRoster
        agents={afterRemoval}
        defaultAgentName="vibey"
        selected={{ kind: 'agent', name: 'cso' }}
        detail={<AgentDetail agent={csoAgent()} isDefault={false} />}
      />,
    ),
  },
  {
    name: 'fresh',
    title: '伙伴与 Agents · 新安装：只有歪比，选中主伙伴',
    width: 1440,
    height: 900,
    status: 'proposed',
    route: '/agents',
    fixtures: fresh,
    render: desktop(
      <AgentsRoster
        agents={builtinAgents}
        defaultAgentName="vibey"
        selected={{ kind: 'bot', handle: 'main' }}
        detail={<BotDetail handle="main" agent={detail('main', 0)} />}
      />,
    ),
  },
  {
    name: 'member',
    title: '伙伴与 Agents · 成员：没有伙伴区',
    width: 1440,
    height: 900,
    status: 'proposed',
    route: '/agents',
    fixtures: member,
    render: desktop(
      roster({
        selected: { kind: 'agent', name: 'reviewer' },
        detail: <AgentDetail agent={detail('reviewer', 120)} isDefault={false} />,
      }),
    ),
  },
  {
    name: 'phone',
    title: '伙伴与 Agents · 手机：列表',
    width: 390,
    height: 844,
    status: 'proposed',
    route: '/agents',
    fixtures: team,
    render: phone(roster({})),
  },
  {
    name: 'phone-bot',
    title: '伙伴与 Agents · 手机：伙伴详情',
    width: 390,
    height: 844,
    status: 'proposed',
    route: '/agents',
    fixtures: team,
    render: phone(roster({ selected: { kind: 'bot', handle: 'cso' }, detail: csoPanel, detailOpen: true })),
  },
]);

export default designFrames;
