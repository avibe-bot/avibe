// The conversations the hold-to-talk frames draw: the security lead's (a Bot) and the cache-fix
// Session in avibe (an ordinary Session). Speech is available, so the composer shows its mic.
import type { WorkbenchMessage } from '@/context/ApiContext';

import { botConversationApi, message, session, type WorkbenchFixtures } from '../../shell/fixtures';
import { agentsApi, csoTranscript, team, teamFixtures } from '../../shell/teamFixtures';

const cso = team.find((each) => each.handle === 'cso')!;

/** The placeholder that teaches the gesture, wherever a hold would record. While a Bot works, its
 *  conversation names it and says nothing about a queue: a message to a Bot always goes straight in
 *  (owner, 2026-11-02), typed or spoken, and a hold still records. */
const holdCopy = {
  'chat.compose.placeholder': { zh: '输入消息，或按住说话', en: 'Message, or hold to talk' },
};
const botCopy = {
  ...holdCopy,
  'chat.compose.placeholderBusy': { zh: '首席安全官正在处理…', en: 'Chief Security Officer is working…' },
};

/** A file staged in the composer with no text: a hold still records, and it goes with the message. */
export const staged = 'avault-依赖扫描.png';

// No Cloud token is minted in a frame: the composer only prewarms it.
const speech = { '/api/asr/status': { available: true }, '/api/cloud/token': { ok: false } };

/** What the user says to the security lead, as the recognizer streams it. */
export const said = {
  text: '令牌刷新那条先修吧，修完跑一遍回归，',
  stash: '有问题直接找产品经理',
  final: '令牌刷新那条先修吧，修完跑一遍回归，有问题直接找产品经理。',
};

/** A take that runs on: the transcript outgrows the card and scrolls. */
export const saidLong = {
  text:
    '令牌刷新那条先修吧，修完跑一遍回归，有问题直接找产品经理。另外登录页那个第三方脚本也看一下，' +
    '能不能换成我们自己托管的版本，换之前先把影响范围列给我。还有 avault 的依赖升级，' +
    '如果要动到签名那部分，先别合，等我周一看过再说。依赖扫描也加进每晚的任务里，扫出高危直接告诉我，' +
    '不用等周报。最后把这周的审计结论整理成一页，',
  stash: '发到产品经理那边一份',
};

/** What the user says in the cache-fix Session. */
export const saidSession = {
  text: '把清理旧工具结果那段加个回归测试，',
  stash: '然后跑一下全量',
};

/** What is typed when a press must stay the system's. */
export const typed = '帮我看一下令牌刷新的修复方案';

/** The security lead's conversation, with the composer as the frame needs it. */
export function csoFixtures({
  messages = csoTranscript,
  running = false,
  draft = '',
  copy = true,
}: { messages?: WorkbenchMessage[]; running?: boolean; draft?: string; copy?: boolean } = {}): WorkbenchFixtures {
  const api = botConversationApi(cso, team, messages, running);
  const bootstrap = api[`/api/sessions/${cso.session_id}/bootstrap`] as Record<string, unknown>;
  return {
    ...teamFixtures,
    botStatus: { ...teamFixtures.botStatus, ...(running ? { [cso.session_id]: 'running' as const } : {}) },
    unreadBySession: { ses_main: 1, ses_demo_cache: 3 },
    api: {
      ...agentsApi,
      ...api,
      [`/api/sessions/${cso.session_id}/bootstrap`]: { ...bootstrap, draft: { text: draft, updated_at: null } },
      ...speech,
    },
    copy: copy ? botCopy : undefined,
  };
}

/** The conversation once the spoken message is in: the security lead is on it. */
export const csoSentMessages = [...csoTranscript, message('ses_cso', 'c3', 'user', said.final, 0)];

const turnState = { in_flight: false, foreground: 'idle', native_turn_started: false, pending_input_count: 0, background_activities: [], pending_activity_output_count: 0, connection: 'connected' };

/** An ordinary Session in a project: the cache fix in avibe. */
export function sessionFixtures(): WorkbenchFixtures {
  const id = 'ses_demo_cache';
  const row = session(id, 'proj_avibe', '修复中途断缓存', { workdir: '/Users/demo/avibe' });
  const messages = [
    message(id, 's1', 'user', '中途断缓存的问题定位一下，先别改代码。', 40),
    message(id, 's2', 'agent', '定位到了：清理旧工具结果时把消息前缀截断了，下一轮请求就命中不了缓存。修复只需要保留前缀，PR 已开。', 21),
  ];
  return {
    ...teamFixtures,
    unreadBySession: { ses_main: 1, ses_cso: 2 },
    api: {
      ...agentsApi,
      [`/api/sessions/${id}`]: row,
      [`/api/sessions/${id}/bootstrap`]: {
        session: row,
        capabilities: { can_chat: true },
        agents: (agentsApi['/api/agents'] as { agents: unknown[] }).agents,
        default_agent_name: 'vibey',
        config: { ui: {} },
        messages,
        queued: [],
        next_after_id: null,
        next_before_id: null,
        draft: { text: '', updated_at: null },
        turn_state: turnState,
      },
      [`/api/sessions/${id}/turn-state`]: turnState,
      [`/api/show-pages/${id}/access`]: {
        ok: true, mode: 'unmanaged', ownership_status: 'unmanaged', access_level: 'private',
        can_use: true, can_manage: false, can_publish_public: false, group_ids: [],
      },
      '/api/vault/requests': { ok: true, requests: [] },
      ...speech,
    },
    copy: holdCopy,
  };
}
