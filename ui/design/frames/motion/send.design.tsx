// Sending a message (docs/plans/web-ui-motion.md M1, M2), as shipped: the shipped ChatPage with a draft
// typed. Sending (the screenshot clicks Send; in the canvas, press it) plays the shipped motion: the
// message rises out of the composer while the conversation glides up, Send turns into Stop, and the
// working bubble follows a beat later. A refused send goes back the way it came and the text is back in
// the composer. Each frame settles where its moment ends.
import { frames } from '../../frame';
import { chatDesktop, chatPhone } from '../chat/fixtures';
import { CHAT, chatStates, mergeFixtures, sent } from './fixtures';

const base = `/api/sessions/${CHAT.before}`;
// Sending saves the composer's now empty draft.
const draft = { [`${base}/draft`]: { ok: true } };
const accepted = mergeFixtures(chatStates.before, { api: { ...draft, [`POST ${base}/messages`]: { ...sent, id: 'msg_sent_standard', session_id: CHAT.before } } });
const refused = mergeFixtures(chatStates.before, {
  api: {
    ...draft,
    [`POST ${base}/messages`]: new Response(JSON.stringify({ error: 'Agent 暂时不可用，请稍后再发。' }), { status: 503, headers: { 'content-type': 'application/json' } }),
  },
});

const press = { click: ['button:has(.lucide-send)'], wait: 600 };
const phone = { width: 390, height: 844, status: 'shipped' as const, route: `/chat/${CHAT.before}`, render: chatPhone, ...press };

const designFrames = frames([
  { ...phone, name: 'phone', title: '发送 · 手机', fixtures: accepted },
  { ...phone, name: 'phone-reduced', title: '发送 · 减少动态效果 · 手机', fixtures: accepted, reducedMotion: true },
  { ...phone, name: 'phone-failed', title: '发送失败，消息退回输入框 · 手机', fixtures: refused },
  { ...phone, name: 'desktop', title: '发送 · 桌面', width: 1280, height: 800, fixtures: accepted, render: chatDesktop },
]);

export default designFrames;
