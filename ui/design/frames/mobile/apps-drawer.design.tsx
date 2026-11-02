// The phone's Apps tab: the Dock drawer over the current page, and the sections its More sheet holds.
import { MobileDockDrawer } from '@/components/apps/MobileDockDrawer';
import { InboxPage } from '@/components/workbench/InboxPage';
import { MoreAppearanceSection, MoreConnectionSection } from '@/components/workbench/MorePage';

import { frames } from '../../frame';
import { showPagesApi } from '../show-pages/fixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';

const designFrames = frames([
  {
    name: 'open',
    title: '手机 · 应用抽屉',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: workbenchFixtures(showPagesApi()),
    render: phoneShell(
      '/inbox',
      <>
        <InboxPage onOpenSearch={() => {}} />
        <MobileDockDrawer open onClose={() => {}} />
      </>,
    ),
  },
  {
    name: 'more',
    title: '手机 · 更多（外观与连接，更多面板的内容）',
    width: 390,
    height: 360,
    status: 'shipped',
    fixtures: workbenchFixtures(),
    render: () => (
      <div className="flex h-full flex-col gap-3 bg-background p-4">
        <MoreAppearanceSection />
        <MoreConnectionSection />
      </div>
    ),
  },
]);

export default designFrames;
