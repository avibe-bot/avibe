// Into a conversation from the Inbox and back, on a phone (docs/plans/web-ui-motion.md M5).
import { frames } from '../../frame';
import { inboxFixtures, TIER_TITLE } from './fixtures';
import { NavigationLoop } from './NavigationDemo';

const designFrames = frames(
  (['now', 'restrained', 'standard', 'pronounced', 'reduced'] as const).map((tier) => ({
    name: `phone-push-${tier}`,
    title: `从收件箱进入会话再返回 · ${TIER_TITLE[tier]} · 手机`,
    width: 390,
    height: 844,
    status: 'proposed' as const,
    route: '/inbox',
    fixtures: inboxFixtures('replied'),
    render: () => <NavigationLoop tier={tier} />,
  })),
);

export default designFrames;
