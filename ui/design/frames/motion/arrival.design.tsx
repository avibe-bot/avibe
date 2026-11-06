// A reply arriving (docs/plans/web-ui-motion.md M3, M4), as shipped: the shipped ChatPage with the agent
// at work, and the reply arriving over the event stream a moment after the frame opens. The working
// bubble gives way to the reply in the same place and the conversation glides up by what it adds; Stop
// hands back to Send. The same motion carries a cached conversation when the live answer brings rows it
// did not have. Each frame settles on the reply.
import { frames } from '../../frame';
import { chatPhone } from '../chat/fixtures';
import { CHAT, chatStates, history, mergeFixtures, reply, sent } from './fixtures';
import { idleTurn } from '../chat/fixtures';

/** When the reply arrives after the frame opens. */
const ARRIVES = 1200;
const base = `/api/sessions/${CHAT.after}`;
const arriving = {
  // The page opens on the turn running; asked again once the reply is in, the server says it ended.
  ...mergeFixtures(chatStates.after, {
    api: {
      [`${base}/turn-state`]: idleTurn,
      [`${base}/messages`]: { messages: [...history, sent, reply].map((m) => ({ ...m, session_id: CHAT.after })), next_after_id: null, next_before_id: null },
    },
  }),
  stream: [
    { after: ARRIVES, event: 'message.new', data: { ...reply, session_id: CHAT.after } },
    { after: ARRIVES, event: 'turn.end', data: { session_id: CHAT.after } },
  ],
};

const phone = { width: 390, height: 844, status: 'shipped' as const, route: `/chat/${CHAT.after}`, fixtures: arriving, render: chatPhone, wait: ARRIVES + 800 };

const designFrames = frames([
  { ...phone, name: 'phone', title: '回复到达（缓存换成实时数据时同样适用） · 手机' },
  { ...phone, name: 'phone-reduced', title: '回复到达 · 减少动态效果 · 手机', reducedMotion: true },
]);

export default designFrames;
