// The desktop ⌘K search palette over the Workbench, as it opens.
import { SearchPalette } from '@/components/workbench/search/SearchPalette';

import { frames } from '../../frame';
import { workbenchFixtures } from '../workbench/fixtures';

const designFrames = frames([
  {
    name: 'open',
    title: '搜索面板 · ⌘K 打开',
    width: 1192,
    height: 640,
    status: 'shipped',
    fixtures: workbenchFixtures(),
    render: () => <SearchPalette open onClose={() => {}} />,
  },
]);

export default designFrames;
