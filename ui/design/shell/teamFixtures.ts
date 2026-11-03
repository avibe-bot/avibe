// The Bot team the mobile Bots designs share: the main Bot with one unread reply,
// the security lead with two, the PM at work, and a fourth Bot that is idle.
import { VOICE_MESSAGE_METADATA } from '@/lib/voiceMessage';

import type { WorkbenchFixtures } from './fixtures';
import { bot, demoBots, demoProjects, demoSessions, inboxRow, mainBot, message, researchBot } from './fixtures';

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
  // The composer asks whether voice input is available; frames draw it without Cloud voice.
  '/api/asr/status': { available: false },
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


/** More Bots than the strip shows at once: it scrolls sideways and Add stays pinned. */
export const manyBots = [
  ...team,
  bot('finance', '财务助理', '账单、报销与预算', null, 4),
  bot('designer', 'Designer', 'Interface and visual design', { kind: 'emoji', emoji: '🎨', hue: 330 }, 5),
  bot('writer', '文案', '发布说明与对外文案', { kind: 'emoji', emoji: '✍️', hue: 150 }, 6),
  bot('legal', '法务', '合同与开源许可审查', { kind: 'emoji', emoji: '⚖️', hue: 12 }, 7),
];

export const manyFixtures: WorkbenchFixtures = {
  ...teamFixtures,
  bots: manyBots,
  botStatus: { ses_pm: 'running', ses_writer: 'running' },
  unreadBySession: { ses_main: 1, ses_cso: 2, ses_legal: 1, ses_demo_cache: 3 },
};

/** What the user says while holding the main Bot, as the recognizer streams it. */
export const spoken = {
  text: '帮我看一下周一发布前还差哪些安全审查，',
  stash: '有问题直接约首席安全官',
  final: '帮我看一下周一发布前还差哪些安全审查，有问题直接约首席安全官。',
  /** A take that runs on: more than the card shows, so it scrolls. */
  long: '帮我看一下周一发布前还差哪些安全审查，有问题直接约首席安全官。另外把发布清单里还没勾的几项列给我，按负责人分一下，今天下班前要看到。欢迎流程的文案我觉得还可以再短一点，你让产品经理出两个版本，明天上午我们一起挑。还有试用登录的令牌刷新，安全那边说要先升级依赖，这个排在发布前面，',
};

/** The main Bot's conversation right after the voice message went in: it is already working on it. */
export const afterVoiceTranscript = [
  ...mainTranscript,
  // Sent by holding the main Bot: the message says it was spoken.
  { ...message('ses_main', 'm4', 'user', spoken.final, 0), metadata: VOICE_MESSAGE_METADATA },
];
