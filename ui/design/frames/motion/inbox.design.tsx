// The Inbox when a reply lands in a conversation lower down (docs/plans/web-ui-motion.md M7, and M4 for a
// cached list replaced by the live one): the card moves to the top, the cards it overtakes glide down.
import { frames } from '../../frame';
import { inboxFixtures, TIER_TITLE } from './fixtures';
import { InboxMoveLoop } from './InboxDemo';

const designFrames = frames(
  (['now', 'restrained', 'standard', 'pronounced', 'reduced'] as const).map((tier) => ({
    name: `phone-move-${tier}`,
    title: `收件箱 · 新回复把会话移到最上面 · ${TIER_TITLE[tier]} · 手机`,
    width: 390,
    height: 844,
    status: 'proposed' as const,
    route: '/inbox',
    fixtures: inboxFixtures(),
    render: () => <InboxMoveLoop tier={tier} />,
  })),
);

export default designFrames;
