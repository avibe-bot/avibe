// The Bot team the mobile Bots designs share: the main Bot with one unread reply,
// the security lead with two, the PM at work, and a fourth Bot that is idle.
import type { WorkbenchFixtures } from './fixtures';
import { demoBots, demoProjects, demoSessions, inboxRow, mainBot, message, researchBot } from './fixtures';

export const team = [...demoBots, researchBot];

export const teamInbox = [
  inboxRow('ses_main', null, '', '周一发布前还差一次安全审查，要我约首席安全官吗？', 4, 1),
  inboxRow('ses_cso', null, '首席安全官', 'avault 依赖里发现 2 个高危漏洞，已整理修复建议和影响范围。', 12, 2),
  inboxRow('ses_pm', null, '产品经理', '需求池按优先级排好了，前三项需要你拍板。', 2),
  inboxRow('ses_research', null, '市场研究员', '本周竞品动态：两家发布了桌面 Agent。', 180),
  inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR 已开。', 21, 3),
];

/** The Agents the composer's route picker lists: the built-in worker and each Bot's Agent. */
export const agentsApi = {
  '/api/agents': {
    ok: true,
    default_agent_name: 'vibey',
    agents: [
      { id: 'agent_vibey', name: 'vibey', backend: 'vibey', enabled: true, archived: false, source: 'builtin', model: 'gpt-6-astra', reasoning_effort: 'high', description: 'Default Agent for the Vibey backend.', bot: null },
      ...team.map((each) => ({
        id: `agent_${each.handle}`, name: each.agent_name, backend: 'vibey', enabled: true, archived: false,
        source: each.builtin ? 'builtin' : 'user', model: 'gpt-6-astra', reasoning_effort: 'high', description: each.role,
        bot: { handle: each.handle, display_name: each.display_name, builtin: each.builtin },
      })),
    ],
  },
  '/api/workbench/prefs': { ok: true, background_work_banner_enabled: true, fast_mode_default: false },
  '/api/models/agents/vibey/models': { ok: false },
};

/** Several Bots, unread on two of them, the PM running. */
export const teamFixtures: WorkbenchFixtures = {
  api: agentsApi,
  projects: demoProjects,
  sessionsByProject: demoSessions,
  bots: team,
  botStatus: { ses_pm: 'running' },
  unreadBySession: { ses_main: 1, ses_cso: 2, ses_demo_cache: 3 },
  inboxSessions: teamInbox,
};

/** The same team while the main Bot itself is working. */
export const mainRunningFixtures: WorkbenchFixtures = {
  ...teamFixtures,
  botStatus: { ses_main: 'running', ses_pm: 'running' },
  unreadBySession: { ses_cso: 2, ses_demo_cache: 3 },
};

/** A new install: the main Bot alone, nothing unread. */
export const aloneFixtures: WorkbenchFixtures = {
  projects: demoProjects,
  sessionsByProject: demoSessions,
  bots: [mainBot],
  inboxSessions: [
    inboxRow('ses_main', null, '', '早，今天想先推进哪件事？', 30),
    inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR 已开。', 21, 3),
  ],
  unreadBySession: { ses_demo_cache: 3 },
};

/** The main Bot's conversation: this morning's exchange. */
export const mainTranscript = [
  message('ses_main', 'm1', 'user', '早，今天有什么安排？', 9),
  message('ses_main', 'm2', 'agent', '欢迎流程周一上线：产品经理在更新发布清单，新的试用登录还差一次安全审查。我已经把审查交给首席安全官，今天会有结果。', 8),
  message('ses_main', 'm3', 'agent', '周一发布前还差一次安全审查，要我约首席安全官吗？', 4),
];

/** The security lead's conversation. */
export const csoTranscript = [
  message('ses_cso', 'c1', 'user', '帮我检查一下新的试用登录，周一前给结论。', 40),
  message('ses_cso', 'c2', 'agent', 'avault 依赖里发现 2 个高危漏洞，已整理修复建议和影响范围：令牌刷新路径需要先升级，登录本身没有问题。', 12),
];

