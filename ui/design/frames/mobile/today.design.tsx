// Bots on a phone today: the shipped tab bar has no way to them, and a Bot's
// conversation is reached from the Inbox's Bots section only.
import { InboxPage } from '@/components/workbench/InboxPage';

import { frames } from '../../frame';
import { PhoneScreen, ShippedTabBar } from '../../shell/PhoneScreen';
import { teamFixtures } from '../../shell/teamFixtures';

const designFrames = frames([
  {
    name: 'inbox',
    title: '手机 · 现状：伙伴只在收件箱里',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures: teamFixtures,
    route: '/inbox',
    render: () => (
      <PhoneScreen tabBar={<ShippedTabBar inboxBadge={6} />}>
        <InboxPage onOpenSearch={() => {}} />
      </PhoneScreen>
    ),
  },
]);

export default designFrames;
