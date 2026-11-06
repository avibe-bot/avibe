// What arrives in the conversation (docs/plans/web-ui-motion.md M3, M4), as shipped: the shipped ChatPage,
// and something arriving over the event stream a moment after the frame opens. Every item enters by the
// side it sits on: on the left (a reply, the running card, an execution-process chip) it grows in place
// from its top-left corner; on the right (a Show Page card sent from here, as a sent message) it comes up
// from the composer. The working bubble gives way to the reply, or to the running card, in the same place,
// and the conversation glides up by what arrives. What a read brings (a cached conversation replaced by
// the live one, a catch-up after the stream was down) lands still. Each frame settles where its moment
// ends.
import { frames } from '../../frame';
import { activityGroup, chatFixtures, chatPhone, idleTurn, message, runningTurn, type ChatFixture } from '../chat/fixtures';
import { CHAT, chatStates, history, mergeFixtures, reply, sent } from './fixtures';

/** When what arrives arrives after the frame opens. */
const ARRIVES = 1200;
const base = (id: string) => `/api/sessions/${id}`;
/** The conversation once the reply is in, as a read of it answers. */
const afterReply = { messages: [...history, sent, reply].map((m) => ({ ...m, session_id: CHAT.after })), next_after_id: null, next_before_id: null };

// ----- The reply -----

const arriving = {
  // The page opens on the turn running; asked again once the reply is in, the server says it ended.
  ...mergeFixtures(chatStates.after, {
    api: {
      [`${base(CHAT.after)}/turn-state`]: idleTurn,
      [`${base(CHAT.after)}/messages`]: afterReply,
    },
  }),
  stream: [
    { after: ARRIVES, event: 'message.new', data: { ...reply, session_id: CHAT.after } },
    { after: ARRIVES, event: 'turn.end', data: { session_id: CHAT.after } },
  ],
};

// ----- The running card, and the turn it ends -----

/** The agent's first step, over the stream while the execution process is shown. */
const step = { ...message('agent', 'tool_call', 'Read ui/src/lib/motion.ts', 0), id: 'act_motion_step', session_id: CHAT.after };
/** The conversation just sent, with the execution process shown (Settings › Chat). */
const withProcess = (fixture: Pick<ChatFixture, 'turnState' | 'activity' | 'api'>) =>
  chatFixtures({ session: { title: 'Inbox 卡顿与动效', id: CHAT.after }, messages: [...history, sent], ui: { show_agent_activity: true }, ...fixture });
const working = {
  ...withProcess({ turnState: runningTurn }),
  stream: [{ after: ARRIVES, event: 'message.new', data: step }],
};
// The turn ends: its reply, and the settled chip above it, which the server lists once the turn is over.
const ending = {
  ...withProcess({
    turnState: runningTurn,
    activity: [activityGroup('grp_motion_turn', reply, [], { steps: 1, duration_ms: 9_000 })],
    api: { [`${base(CHAT.after)}/turn-state`]: idleTurn, [`${base(CHAT.after)}/messages`]: afterReply },
  }),
  stream: [
    { after: 300, event: 'message.new', data: step },
    { after: ARRIVES, event: 'message.new', data: { ...reply, session_id: CHAT.after } },
    { after: ARRIVES, event: 'turn.end', data: { session_id: CHAT.after } },
  ],
};

// ----- A Show Page card sent from here -----

const card = {
  ...message('harness', 'annotation', '标题太长了，压到一行。', 0, {
    content: { annotation: { direction: 'user', action: 'created', quote: '发送、停止和工作中这三个过渡' } },
  }),
  session_id: CHAT.replied,
};
const annotating = {
  ...chatStates.replied,
  stream: [{ after: ARRIVES, event: 'message.new', data: card }],
};

const phone = { width: 390, height: 844, status: 'shipped' as const, render: chatPhone, wait: ARRIVES + 800 };
const after = { ...phone, route: `/chat/${CHAT.after}` };

const designFrames = frames([
  { ...after, name: 'phone', title: '回复到达 · 手机', fixtures: arriving },
  { ...after, name: 'phone-reduced', title: '回复到达 · 减少动态效果 · 手机', fixtures: arriving, reducedMotion: true },
  { ...after, name: 'phone-card', title: '执行过程开始：运行卡片接替工作气泡 · 手机', fixtures: working },
  { ...after, name: 'phone-turn-end', title: '一轮结束：回复和执行过程同时长出，运行卡片淡出 · 手机', fixtures: ending },
  { ...phone, name: 'phone-show-page', route: `/chat/${CHAT.replied}`, title: '从这里发出的 Show Page 批注：从输入框升起 · 手机', fixtures: annotating },
]);

export default designFrames;
