// Into a conversation from the Inbox and back, on a phone (docs/plans/web-ui-motion.md M5), approved on
// 2026-11-05: the card answers the tap, the conversation is pushed in from the right while the Inbox drifts
// the other way, and Back reverses it. View Transitions on iOS 18 and later; on iOS 17 a cut.
import { frames } from '../../frame';
import { NavigationLoop } from '../../kit/motion/NavigationMoment';
import { CHAT, inboxFixtures } from './fixtures';

const phone = { width: 390, height: 844, status: 'approved' as const, route: '/inbox', fixtures: inboxFixtures('replied') };

const designFrames = frames([
  { ...phone, name: 'phone-push', title: '从收件箱进入会话再返回 · 手机', render: () => <NavigationLoop session={CHAT.replied} /> },
  { ...phone, name: 'phone-push-reduced', title: '进入会话再返回 · 减少动态效果 · 手机', render: () => <NavigationLoop session={CHAT.replied} reduced /> },
]);

export default designFrames;
