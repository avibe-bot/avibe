// Sending a message (docs/plans/web-ui-motion.md M1, M2), approved by the owner on 2026-11-05: the
// message rises out of the composer while the conversation glides up, Send turns into Stop, and the
// working bubble follows a beat later; a failed send goes back the way it came. Each with its
// reduced-motion form.
import { frames } from '../../frame';
import { FailLoop, SendLoop } from '../../kit/motion/SendMoment';
import { CHAT, CHAT_CAST, chatStatesFixtures, SHIFT } from './fixtures';

const phone = { width: 390, height: 844, status: 'approved' as const, route: `/chat/${CHAT.after}`, fixtures: chatStatesFixtures('before', 'after') };
const desktop = { ...phone, width: 1280, height: 800 };

const designFrames = frames([
  { ...phone, name: 'phone', title: '发送 · 手机', render: () => <SendLoop cast={CHAT_CAST} shift={SHIFT.phone.send} /> },
  { ...phone, name: 'phone-reduced', title: '发送 · 减少动态效果 · 手机', render: () => <SendLoop cast={CHAT_CAST} shift={SHIFT.phone.send} reduced /> },
  { ...phone, name: 'phone-failed', title: '发送失败，消息退回输入框 · 手机', render: () => <FailLoop cast={CHAT_CAST} shift={SHIFT.phone.send} /> },
  { ...desktop, name: 'desktop', title: '发送 · 桌面', render: () => <SendLoop cast={CHAT_CAST} shift={SHIFT.desktop.send} desktop /> },
]);

export default designFrames;
