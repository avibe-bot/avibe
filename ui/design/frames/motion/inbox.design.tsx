// The Inbox when a reply lands in a conversation lower down (docs/plans/web-ui-motion.md M7, and M4 for a
// cached list replaced by the live one), approved on 2026-11-05: the card leaves its slot, the cards above
// glide down, and it comes in at the top.
import { frames } from '../../frame';
import { InboxMoveLoop } from '../../kit/motion/InboxMoment';
import { INBOX_MOVE, inboxFixtures } from './fixtures';

const phone = { width: 390, height: 844, status: 'approved' as const, route: '/inbox', fixtures: inboxFixtures() };

const designFrames = frames([
  { ...phone, name: 'phone-move', title: '收件箱 · 新回复把会话移到最上面 · 手机', render: () => <InboxMoveLoop cast={INBOX_MOVE} /> },
  { ...phone, name: 'phone-move-reduced', title: '收件箱 · 移到最上面 · 减少动态效果 · 手机', render: () => <InboxMoveLoop cast={INBOX_MOVE} reduced /> },
]);

export default designFrames;
