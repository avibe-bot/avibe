// The phone's ＋: the new session sheet over the current page — pick a project and an Agent, then type.
import { InboxPage } from '@/components/workbench/InboxPage';
import { NewSessionSheet } from '@/components/workbench/NewSessionSheet';

import { frames } from '../../frame';
import { agentsApi } from '../agents/fixtures';
import { phoneShell, workbenchFixtures } from '../workbench/fixtures';

const designFrames = frames([
  {
    name: 'open',
    title: '手机 · 新建会话',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/inbox',
    fixtures: workbenchFixtures(agentsApi()),
    render: phoneShell(
      '/inbox',
      <>
        <InboxPage onOpenSearch={() => {}} />
        <NewSessionSheet open onClose={() => {}} onOpen={() => {}} />
      </>,
    ),
  },
]);

export default designFrames;
