// What the motion frames share (docs/plans/web-ui-motion.md): one real conversation and one real Inbox,
// and the selectors a choreography moves. A moment is the change between two states the product already
// draws, so a frame stacks the shipped page in each state (`stage.tsx`) and the loop in `kit/motion`
// moves between them: the page after the change starts where the page before it was, and glides to where
// it is. Every comparison uses the same data and the same size, tier by tier.
import type { InboxState } from '@/context/WorkbenchInboxContext';
import { unreadByCategory } from '@/lib/notificationPreferences';

import { demoBots, inboxRow, type WorkbenchFixtures } from '../../shell/fixtures';
import { agentSays, chatFixtures, idleTurn, runningTurn, userSays } from '../chat/fixtures';
import { workbenchFixtures } from '../workbench/fixtures';

/** How strongly a moment moves. `now` is what the product does today; the plan recommends `standard`. */
export type Tier = 'now' | 'restrained' | 'standard' | 'pronounced' | 'reduced';

export const TIER_TITLE: Record<Tier, string> = {
  now: '现在',
  restrained: '克制',
  standard: '标准（推荐）',
  pronounced: '明显',
  reduced: '减少动态效果',
};

/** Several fixture sets in one frame: one page per state, each reading its own Session. */
export function mergeFixtures(...sets: WorkbenchFixtures[]): WorkbenchFixtures {
  return { ...sets[0], api: Object.assign({}, ...sets.map((set) => set.api)) };
}

// ----- The conversation -----

export const history = [
  userSays('帮我查一下 Inbox 在 Safari 上的卡顿，新消息进来时最明显。', 50),
  agentSays(
    '查到了：每来一条实时更新，40 张卡片都会重新解析一遍 Markdown 预览。改成按预览文本缓存，并且只把开头 1200 个字符交给 Markdown：\n\n- 4× 降速下，长任务从 **46 个降到 0**\n- 收件箱单次渲染从 101 ms 降到 6 ms\n\nPR #3256 已经开好，CI 在跑。',
    44,
  ),
  userSays('很好。再看看手机上打开会话时的跳动。', 30),
  agentSays(
    '打开会话时先画缓存里的内容，实时数据到了再替换。跳动来自新消息直接插进来：整段内容一下子上移一截，输入框里的按钮也是瞬间换掉的。\n\n我把每个时刻的过渡写进了动效方案，每个给了三档强度，你挑一个。',
    24,
  ),
];

/** What the reader types and sends. */
export const SENT_TEXT = '先按「标准」做，做完开 PR。';
export const sent = userSays(SENT_TEXT, 0);
/** The reply that ends the turn the message started. */
export const reply = agentSays('好，按「标准」档实现。第一步是发送、停止和工作中这三个过渡，我先把动效令牌接进输入框和消息列表。', 0);

/** How far the conversation moves when rows go in below it: the row heights the shipped page draws for
 *  this data (FLIP measures them at run time; a frame has to state them). `send`: the message and the
 *  working bubble; `arrival`: what the reply adds over the bubble it replaces. */
export const SHIFT = {
  phone: { send: 147, arrival: 76 },
  desktop: { send: 66, arrival: 31 },
} as const;

/** The Session ids each state reads (ids as the server mints them). */
export const CHAT = {
  before: 'sesmo7b4f0re1',
  waiting: 'sesmo7w4i7in2',
  after: 'sesmo7a4f7er3',
  replied: 'sesmo7r3p1ed4',
} as const;
export type ChatState = keyof typeof CHAT;

const title = { title: 'Inbox 卡顿与动效' };

/** Each state of the conversation, as its bootstrap serves it. */
export const chatStates: Record<ChatState, WorkbenchFixtures> = {
  // A draft typed and ready to send.
  before: chatFixtures({ session: { ...title, id: CHAT.before }, messages: history, draft: SENT_TEXT, turnState: idleTurn }),
  // Today: the turn starts at once, the message only lands when the POST answers.
  waiting: chatFixtures({ session: { ...title, id: CHAT.waiting }, messages: history, turnState: runningTurn }),
  // Sent: the message is in, the agent is working.
  after: chatFixtures({ session: { ...title, id: CHAT.after }, messages: [...history, sent], turnState: runningTurn }),
  // The reply has landed and the turn is over.
  replied: chatFixtures({ session: { ...title, id: CHAT.replied }, messages: [...history, sent, reply], turnState: idleTurn }),
};

/** The fixtures for a frame that stacks these states. */
export const chatStatesFixtures = (...states: ChatState[]) => mergeFixtures(...states.map((state) => chatStates[state]));

// ----- Selectors into the shipped chat page -----

/** Inside one stacked state. */
export const inLayer = (layer: string, selector = '') => `[data-motion-layer="${layer}"] ${selector}`.trim();
/** The transcript's scrolling viewport, and the column of rows inside it. */
export const TRANSCRIPT = '[data-testid="chat-transcript"]';
export const TRANSCRIPT_ROWS = `${TRANSCRIPT} > div`;
/** One message row by id. */
export const row = (id: string) => `[data-message-id="${id}"]`;
/** The agent-working bubble: the transcript's tail while a turn runs. */
export const WORKING = `${TRANSCRIPT_ROWS} > :last-child`;
/** The chat header. */
export const HEADER = '[data-chat-header]';
/** The composer: the chat surface's last child, below the transcript. */
export const COMPOSER = `div:has(> div > ${TRANSCRIPT}) > :last-child`;
/** What is typed in the composer (the editor and its placeholder). */
export const TYPED = 'div:has(> [contenteditable])';
/** The composer's Send and Stop buttons, by their icons (so in either language). */
export const SEND = 'button:has(> .lucide-send)';
export const STOP = 'button:has(> .lucide-square)';

// ----- The Inbox -----

type InboxRow = InboxState['inboxSessions'][number];

/** The conversation above, as an Inbox card: its last reply was read 45 minutes ago. */
const ours = (preview: string, minutesAgo: number, unread: number): InboxRow => inboxRow(CHAT.replied, 'avibe', title.title, preview, minutesAgo, unread);

const others = {
  cache: inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR #3141 已开，等你 review。', 12, 3),
  pr: inboxRow('ses_demo_pr', 'avibe', 'PR #3073 交付', 'CI 失败：`unit-tests (2/8)`，是 flaky 的 SQLite 锁超时，已经重跑。', 20, 2),
  docs: inboxRow('ses_demo_docs', 'avibe-docs', '中英文档校对', '发现 3 处中英文不对应，已列出 diff 等你拍板是否直接改。', 180, 1),
};

/** The Inbox (unread, the default tab) before and after the reply above lands: the conversation had one
 *  unread reply and sat third; its new reply moves it to the top with two. No Bot has anything unread, so
 *  the Bots section is not drawn and the list starts at the top. */
export const INBOX = {
  before: {
    rows: [others.cache, others.pr, ours(history[3].text ?? '', 45, 1), others.docs],
    unread: { ses_demo_cache: 3, ses_demo_pr: 2, [CHAT.replied]: 1, ses_demo_docs: 1 },
  },
  after: {
    rows: [ours(reply.text ?? '', 0, 2), others.cache, others.pr, others.docs],
    unread: { ses_demo_cache: 3, ses_demo_pr: 2, [CHAT.replied]: 2, ses_demo_docs: 1 },
  },
} as const;
export type InboxStateName = keyof typeof INBOX;

const botSessions = new Set(demoBots.map((bot) => bot.session_id));

/** The Inbox context a stacked state reads: its rows and counts, and everything else from the frame's. */
export function inboxOverride(base: InboxState, state: InboxStateName): InboxState {
  const { rows, unread } = INBOX[state];
  const counts = Object.values(unread);
  const total = counts.reduce((sum, n) => sum + n, 0);
  const known: Partial<InboxState> = {
    inboxSessions: [...rows],
    unreadBySession: { ...unread },
    totalUnread: total,
    badgeTotal: total,
    unreadSessions: counts.filter((n) => n > 0).length,
    unreadByCategory: unreadByCategory(unread, botSessions),
  };
  return new Proxy(known, { get: (target, key) => (key in target ? target[key as keyof InboxState] : base[key as keyof InboxState]) }) as InboxState;
}

/** The Inbox frames' fixtures: the shell's reads (the Bots, the main one in the tab bar), plus any chat states. */
export const inboxFixtures = (...states: ChatState[]) => mergeFixtures(workbenchFixtures(), ...states.map((state) => chatStates[state]));

/** Where each card of the after list sat in the before list, relative to its new slot (px, at 390 px):
 *  the FLIP offsets the implementation measures as the list changes. */
export const INBOX_FLIP: Record<string, number> = {
  [CHAT.replied]: 369,
  ses_demo_cache: -184,
  ses_demo_pr: -185,
  ses_demo_docs: 21,
};

/** An Inbox card by its Session. */
export const card = (sessionId: string) => `[data-inbox-session-id="${sessionId}"]`;
