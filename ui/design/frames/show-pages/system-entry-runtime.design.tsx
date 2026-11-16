import { ShowPageSystemEntryRuntimePreview } from '../../kit/show-page-system-entry-runtime';
import { frames } from '../../frame';
import { workbenchFixtures } from '../workbench/fixtures';

const designFrames = frames([
  {
    name: 'iphone-expanded',
    title: 'Show system entry · iPhone 竖屏 · 展开指引',
    width: 390,
    height: 844,
    status: 'proposed',
    fixtures: workbenchFixtures(),
    render: () => <ShowPageSystemEntryRuntimePreview mode="ios" state="expanded" />,
  },
  {
    name: 'mac-collapsed',
    title: 'Show system entry · Mac 宽屏 · 收起后的入口',
    width: 1280,
    height: 760,
    status: 'proposed',
    fixtures: workbenchFixtures(),
    render: () => <ShowPageSystemEntryRuntimePreview mode="mac" state="collapsed" />,
  },
]);

export default designFrames;
