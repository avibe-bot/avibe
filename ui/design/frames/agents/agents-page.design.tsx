// The Agents page, Definitions tab, rendered whole: the Bots, then the Agents, with the selected one's panel beside
// them (the states of the approved design are in bots-redesign.design.tsx).
import { AgentsPage } from '@/components/workbench/AgentsPage';

import { frames } from '../../frame';
import { consolePage, phoneShell, workbenchFixtures } from '../workbench/fixtures';
import { agentsApi } from './fixtures';

const page = <AgentsPage />;

const designFrames = frames([
  {
    name: 'definitions',
    title: 'Agents · 列表与选中详情',
    width: 1192,
    height: 1320,
    status: 'shipped',
    route: '/agents?tab=definitions&agent=reviewer',
    fixtures: workbenchFixtures(agentsApi('reviewer')),
    render: consolePage(page),
  },
  {
    name: 'main-bot',
    title: 'Agents · 选中主伙伴',
    width: 1192,
    height: 1320,
    status: 'shipped',
    route: '/agents?tab=definitions&agent=main',
    fixtures: workbenchFixtures(agentsApi('main')),
    render: consolePage(page),
  },
  {
    name: 'phone',
    title: 'Agents · 手机（能力页）',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/agents?tab=definitions',
    fixtures: workbenchFixtures(agentsApi()),
    render: phoneShell('/agents', page),
  },
]);

export default designFrames;
