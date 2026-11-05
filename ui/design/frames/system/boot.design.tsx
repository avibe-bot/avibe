// What a cold start draws while the route guard re-checks the session and nothing is cached: the
// shell's outline (`BootSkeleton`) in place of a centered "Loading…". Beside each, the shell once it
// is in, at the same size. The brand is known, so it is drawn as itself.
import { BootSkeleton } from '@/components/BootSkeleton';
import { Workbench } from '@/components/Workbench';

import { frames } from '../../frame';
import { DesktopScreen } from '../../shell/DesktopScreen';
import { agentsApi } from '../agents/fixtures';
import { homeFactsApi, phoneShell, workbenchFixtures } from '../workbench/fixtures';

const fixtures = workbenchFixtures({
  ...agentsApi(),
  ...homeFactsApi({ model: true, push: true, computerUse: true, chatApps: true, paired: true }),
});

const designFrames = frames([
  {
    name: 'desktop',
    title: '打开 App · 骨架 · 桌面',
    width: 1440,
    height: 900,
    status: 'shipped',
    fixtures,
    render: () => <BootSkeleton />,
  },
  {
    name: 'desktop-loaded',
    title: '打开 App · 骨架对照 · 桌面进入后',
    width: 1440,
    height: 900,
    status: 'shipped',
    fixtures,
    render: () => <DesktopScreen />,
  },
  {
    name: 'phone',
    title: '打开 App · 骨架 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures,
    render: () => <BootSkeleton />,
  },
  {
    name: 'phone-loaded',
    title: '打开 App · 骨架对照 · 手机进入后',
    width: 390,
    height: 844,
    status: 'shipped',
    fixtures,
    render: phoneShell('/', <Workbench />),
  },
]);

export default designFrames;
