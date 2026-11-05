// Sending a message (docs/plans/web-ui-motion.md M1, M2): the message rises out of the composer and
// settles in place while the conversation glides up to make room, Send turns into Stop, and the agent's
// working bubble fades in under it. If the send fails, the message is withdrawn the way it came and the
// text is back in the composer. Each tier is the same conversation at the same size; `now` is what the
// product does today (the turn starts at once, the message lands when the POST answers).
import { frames } from '../../frame';
import { CHAT, chatStatesFixtures, SHIFT, TIER_TITLE } from './fixtures';
import { FailLoop, SendLoop } from './SendDemo';

const phone = { width: 390, height: 844, status: 'proposed' as const, route: `/chat/${CHAT.after}` };

const designFrames = frames([
  ...(['now', 'restrained', 'standard', 'pronounced', 'reduced'] as const).map((tier) => ({
    ...phone,
    name: `phone-${tier}`,
    title: `发送 · ${TIER_TITLE[tier]} · 手机`,
    fixtures: chatStatesFixtures('before', 'waiting', 'after'),
    render: () => <SendLoop tier={tier} shift={SHIFT.phone.send} />,
  })),
  ...(['now', 'standard'] as const).map((tier) => ({
    ...phone,
    name: `phone-failed-${tier}`,
    title: `发送失败，消息退回输入框 · ${TIER_TITLE[tier]} · 手机`,
    fixtures: chatStatesFixtures('before', 'after'),
    render: () => <FailLoop tier={tier} />,
  })),
  ...(['now', 'standard'] as const).map((tier) => ({
    name: `desktop-${tier}`,
    title: `发送 · ${TIER_TITLE[tier]} · 桌面`,
    width: 1280,
    height: 800,
    status: 'proposed' as const,
    route: `/chat/${CHAT.after}`,
    fixtures: chatStatesFixtures('before', 'waiting', 'after'),
    render: () => <SendLoop tier={tier} shift={SHIFT.desktop.send} desktop />,
  })),
]);

export default designFrames;
