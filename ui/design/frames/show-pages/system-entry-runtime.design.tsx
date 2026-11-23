import { ShowSystemEntryFixture } from '../../shell/ShowSystemEntryFixture';
import { frames } from '../../frame';
import { workbenchFixtures } from '../workbench/fixtures';

const designFrames = frames([
  {
    name: 'iphone-collapsed',
    title: 'Show system entry · iPhone · installation and annotation controls',
    width: 390,
    height: 844,
    status: 'shipped',
    browserColorScheme: true,
    fixtures: workbenchFixtures(),
    render: () => <ShowSystemEntryFixture platform="ios" state="collapsed" />,
  },
  {
    name: 'iphone-expanded',
    title: 'Show system entry · iPhone 竖屏 · 展开指引',
    width: 390,
    height: 844,
    status: 'shipped',
    browserColorScheme: true,
    fixtures: workbenchFixtures(),
    render: () => <ShowSystemEntryFixture platform="ios" state="expanded" />,
  },
  {
    name: 'mac-collapsed',
    title: 'Show system entry · Mac 宽屏 · 收起后的入口',
    width: 1280,
    height: 760,
    status: 'shipped',
    browserColorScheme: true,
    fixtures: workbenchFixtures(),
    render: () => <ShowSystemEntryFixture platform="mac" state="collapsed" />,
  },
  {
    name: 'mac-expanded',
    title: 'Show system entry · Mac · expanded above annotation controls',
    width: 1280,
    height: 760,
    status: 'shipped',
    browserColorScheme: true,
    fixtures: workbenchFixtures(),
    render: () => <ShowSystemEntryFixture platform="mac" state="expanded" />,
  },
  {
    name: 'iphone-annotating',
    title: 'Show system entry · iPhone · guidance waits while annotating',
    width: 390,
    height: 844,
    status: 'shipped',
    browserColorScheme: true,
    fixtures: workbenchFixtures(),
    render: () => <ShowSystemEntryFixture platform="ios" state="annotating" />,
  },
]);

export default designFrames;
