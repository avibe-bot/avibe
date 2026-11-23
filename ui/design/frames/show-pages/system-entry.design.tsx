import { ShowPageSystemEntryGuide, type ShowPageSystemEntryMode } from '@/components/workbench/ShowPageSystemEntryGuide';

import { frames } from '../../frame';
import { workbenchFixtures } from '../workbench/fixtures';

const systemEntryFrame = (mode: ShowPageSystemEntryMode) => (
  <div className="flex h-full items-start justify-end bg-background p-6">
    <div className="w-[360px] rounded-xl border border-border bg-surface p-4 shadow-elevation-1">
      <div className="mb-3 text-[13px] font-semibold text-foreground">Q4 Roadmap</div>
      <div className="rounded-lg border border-border bg-background p-3">
        <ShowPageSystemEntryGuide mode={mode} link="/p/q4-roadmap/" />
      </div>
    </div>
  </div>
);

const designFrames = frames([
  {
    name: 'author-share',
    title: 'Show system entry · 作者分享控件',
    width: 520,
    height: 360,
    status: 'shipped',
    fixtures: workbenchFixtures(),
    render: () => systemEntryFrame('ios'),
  },
  {
    name: 'receiver-reader',
    title: 'Show system entry · 接收者阅读页',
    width: 520,
    height: 360,
    status: 'shipped',
    fixtures: workbenchFixtures(),
    render: () => systemEntryFrame('mac'),
  },
  {
    name: 'native-shell',
    title: 'Show system entry · 原生壳引导浏览器',
    width: 520,
    height: 360,
    status: 'shipped',
    fixtures: workbenchFixtures(),
    render: () => systemEntryFrame('native'),
  },
  {
    name: 'browser-unreachable',
    title: 'Show system entry · 浏览器不可达',
    width: 520,
    height: 360,
    status: 'shipped',
    fixtures: workbenchFixtures(),
    render: () => systemEntryFrame('unavailable'),
  },
]);

export default designFrames;
