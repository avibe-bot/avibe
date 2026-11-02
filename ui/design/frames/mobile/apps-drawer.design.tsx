// The phone's Apps tab: the Dock drawer over the current page, and its More sheet (appearance, status).
import { MobileDockDrawer } from '@/components/apps/MobileDockDrawer';
import { InboxPage } from '@/components/workbench/InboxPage';

import { frames } from '../../frame';
import { showPagesApi } from '../show-pages/fixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';

const drawer = phoneShell(
  '/inbox',
  <>
    <InboxPage onOpenSearch={() => {}} />
    <MobileDockDrawer open onClose={() => {}} />
  </>,
);

const designFrames = frames([
  {
    name: 'open',
    title: '手机 · 应用抽屉',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: workbenchFixtures(showPagesApi()),
    render: drawer,
  },
  {
    name: 'more',
    title: '手机 · 应用抽屉 · 更多',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: workbenchFixtures(showPagesApi()),
    click: ['role=button[name="{t:nav.more}"]'],
    render: drawer,
  },
]);

export default designFrames;
