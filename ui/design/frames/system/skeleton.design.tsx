// Loading without a spinner: the shipped skeleton (`components/ui/skeleton`) on the Inbox's first load
// (inbox/inbox-page/loading). The owner chose the light shimmer (2026-11-05): one highlight per card at
// 3% of the foreground, 1.6 s a period, moving about 60% of it. The first frame holds the highlight
// halfway through its sweep so a still shows it; the second is what reduced motion gets, the blocks
// and no sweep. In the canvas and a Show Page the sweep runs.
import { InboxPage } from '@/components/workbench/InboxPage';

import { frames } from '../../frame';
import { SkeletonPose } from '../../shell/SkeletonPose';
import { consolePage, workbenchFixtures } from '../workbench/fixtures';

const loading = workbenchFixtures({}, { inboxLoading: true });

const designFrames = frames([
  {
    name: 'shimmer',
    title: '骨架 · shimmer（轻，3%，停在扫光中段）',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: '/inbox',
    fixtures: loading,
    render: consolePage(
      <SkeletonPose still>
        <InboxPage onOpenSearch={() => {}} />
      </SkeletonPose>,
    ),
  },
  {
    name: 'reduced-motion',
    title: '骨架 · 减少动态效果（静止）',
    width: 1192,
    height: 640,
    status: 'shipped',
    route: '/inbox',
    fixtures: loading,
    render: consolePage(
      <SkeletonPose reduced>
        <InboxPage onOpenSearch={() => {}} />
      </SkeletonPose>,
    ),
  },
]);

export default designFrames;
