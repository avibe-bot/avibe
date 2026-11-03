// The surfaces each visual direction is judged on, drawn by the shipped components inside the shipped
// AppShell: a Bot's conversation with the sidebar and its Bots strip, the home, the Inbox, the Agents
// page, and on a phone the Workbench tab, a Bot's conversation, and the Inbox. `today.design.tsx`
// draws them as they ship; each direction file draws the same frames inside its `<Direction>`.
import type { ReactNode } from 'react';

import { AgentsPage } from '@/components/workbench/AgentsPage';
import { ChatPage } from '@/components/workbench/ChatPage';
import { InboxPage } from '@/components/workbench/InboxPage';
import { ProjectsPage } from '@/components/workbench/ProjectsPage';
import { Workbench } from '@/components/Workbench';

import type { FrameSpec, FrameStatus } from '../../frame';
import { Direction, type DirectionName } from '../../kit/directions/Direction';
import { StateSheet } from '../../kit/directions/StateSheet';
import { botConversationApi, demoSessions, inboxRow, message } from '../../shell/fixtures';
import { PhoneConversation } from '../../shell/PhoneScreen';
import { agentsApi as teamAgentsApi, team, teamFixtures } from '../../shell/teamFixtures';
import { agentsApi } from '../agents/fixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';

const [, cso] = team;

const csoTalk = [
  message('ses_cso', 'c1', 'user', '帮我检查一下新的试用登录，周一前给结论。', 52),
  message(
    'ses_cso',
    'c2',
    'agent',
    '查完了。登录本身没有问题，但 avault 依赖里有 **2 个高危漏洞**，都在令牌刷新路径上：\n\n- `jsonwebtoken` 9.0.0：签名校验可被绕过\n- `axios` 1.6.0：重定向时泄露请求头\n\n升级到 avault 0.7.0 可以一起修掉，影响范围只有刷新令牌这一处。',
    40,
  ),
  message('ses_cso', 'c3', 'user', '好，先开 PR，测试全过再叫我。', 20),
  message('ses_cso', 'c4', 'agent', 'PR #3141 已开，单测和 e2e 都过了。等你 review。', 6),
];

/** The team in the security lead's conversation: the PM at work, the main Bot with one unread reply. */
const inCso = workbenchFixtures(
  { ...teamAgentsApi, ...botConversationApi(cso, team, csoTalk) },
  { ...teamFixtures, unreadBySession: { ses_main: 1, ses_demo_cache: 3 } },
);

const home = workbenchFixtures(agentsApi(), {
  bots: team,
  botStatus: { ses_pm: 'running' },
  sessionsByProject: demoSessions,
  expandedProjects: ['proj_avibe'],
  unreadBySession: { ses_demo_cache: 3, ses_cso: 2 },
  inboxSessions: [
    inboxRow('ses_cso', null, '首席安全官', 'avault 依赖里发现 2 个高危漏洞。', 6, 2),
    inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR 已开。', 12, 3),
  ],
});

const inboxRows = [
  inboxRow('ses_cso', null, '首席安全官', 'avault 依赖里发现 2 个高危漏洞，**升级到 0.7.0** 可以修复，要我开 PR 吗？', 6, 2),
  inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR #3141 已开，等你 review。', 12, 3),
  inboxRow('ses_pm', null, '产品经理', '需求池按优先级排好了，前三项需要你拍板。', 25, 1),
  inboxRow('ses_demo_pr', 'avibe', 'PR #3073 交付', 'CI 失败：`unit-tests (2/8)`，是 flaky 的 SQLite 锁超时，已经重跑。', 40, 2),
];
const inbox = workbenchFixtures(teamAgentsApi, {
  ...teamFixtures,
  inboxSessions: inboxRows,
  unreadBySession: { ses_cso: 2, ses_demo_cache: 3, ses_pm: 1, ses_demo_pr: 2 },
});

const workbenchTab = workbenchFixtures(teamAgentsApi, teamFixtures);

interface Surface {
  name: string;
  title: string;
  width: number;
  height: number;
  route: string;
  fixtures: FrameSpec['fixtures'];
  draw: () => ReactNode;
}

const surfaces: Surface[] = [
  {
    name: 'desktop-bot',
    title: '桌面 · 伙伴对话：侧栏伙伴条选中首席安全官，产品经理运行中',
    width: 1440,
    height: 900,
    route: '/chat/ses_cso',
    fixtures: inCso,
    draw: phoneShell('/chat/:sessionId', <ChatPage />),
  },
  {
    name: 'desktop-home',
    title: '桌面 · 首页',
    width: 1440,
    height: 900,
    route: '/',
    fixtures: home,
    draw: phoneShell('/', <Workbench />),
  },
  {
    name: 'desktop-inbox',
    title: '桌面 · 收件箱',
    width: 1440,
    height: 900,
    route: '/inbox',
    fixtures: inbox,
    draw: phoneShell('/inbox', <InboxPage onOpenSearch={() => {}} />),
  },
  {
    name: 'desktop-agents',
    title: '桌面 · Agents：选中 reviewer',
    width: 1440,
    height: 900,
    route: '/agents?tab=definitions&agent=reviewer',
    fixtures: workbenchFixtures(agentsApi('reviewer')),
    draw: phoneShell('/agents', <AgentsPage />),
  },
  {
    name: 'phone-workbench',
    title: '手机 · 工作台',
    width: 390,
    height: 844,
    route: '/projects',
    fixtures: workbenchTab,
    draw: phoneShell('/projects', <ProjectsPage />),
  },
  {
    name: 'phone-bot',
    title: '手机 · 伙伴对话',
    width: 390,
    height: 844,
    route: '/chat/ses_cso',
    fixtures: inCso,
    draw: () => <PhoneConversation />,
  },
  {
    name: 'phone-inbox',
    title: '手机 · 收件箱',
    width: 390,
    height: 844,
    route: '/inbox',
    fixtures: inbox,
    draw: phoneShell('/inbox', <InboxPage onOpenSearch={() => {}} />),
  },
];

/** Today's surfaces, as they ship. */
export const todayFrames = (): FrameSpec[] =>
  surfaces.map(({ draw, ...surface }) => ({ ...surface, status: 'shipped' as FrameStatus, render: draw }));

/** The same surfaces in one direction, and its state sheet. */
export const directionFrames = (direction: DirectionName, label: string): FrameSpec[] => [
  {
    name: 'states',
    title: `${label} · 状态：选中、运行中、未读、主操作、焦点`,
    width: 760,
    height: 220,
    status: 'proposed',
    render: () => (
      <Direction name={direction}>
        <StateSheet />
      </Direction>
    ),
  },
  ...surfaces.map(({ draw, title, ...surface }) => ({
    ...surface,
    title: `${label} · ${title}`,
    status: 'proposed' as FrameStatus,
    render: () => <Direction name={direction}>{draw()}</Direction>,
  })),
];
