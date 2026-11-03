// The Dock: the sidebar's Apps button reveals it above the button, on hover as a preview and pinned by a
// click. Built-in apps and pinned Show Pages share one row; running apps carry a dot and a ＋ for another
// window, and minimized windows trail the row.
import { Workbench } from '@/components/Workbench';

import { frames } from '../../frame';
import { phoneShell } from '../workbench/fixtures';
import { appsFixtures, pinnedDock, win, windowLayout } from './fixtures';

const home = phoneShell('/', <Workbench />);
const openDock = ['role=button[name="{t:apps.title}"]'];
const pinned = { '/api/dock': { ok: true, dock: pinnedDock() } };
// A Terminal and an Editor are open; a Files window is minimized into the Dock.
const running = windowLayout([
  win(1, 'files', { x: 340, y: 64, width: 860, height: 520 }, { minimized: true }),
  win(2, 'editor', { x: 380, y: 80, width: 900, height: 560 }, { minimized: true, title: 'App.tsx' }),
  win(3, 'terminal', { x: 560, y: 120, width: 780, height: 470 }),
]);

const designFrames = frames([
  {
    name: 'open',
    title: 'Dock · 固定展开（新安装的内置应用）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: appsFixtures(),
    click: openDock,
    render: home,
  },
  {
    name: 'hover',
    title: 'Dock · 悬停「应用」预览',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: appsFixtures(),
    hover: '[aria-haspopup="menu"][aria-pressed]',
    render: home,
  },
  {
    name: 'pinned-pages',
    title: 'Dock · 固定的 Show Page 应用',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: appsFixtures(pinned),
    click: openDock,
    render: home,
  },
  {
    name: 'running',
    title: 'Dock · 运行中的应用与最小化的窗口',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: appsFixtures(pinned, { storage: running }),
    click: openDock,
    wait: 2000,
    render: home,
  },
  {
    name: 'new-window',
    title: 'Dock · 运行中的应用悬停出 ＋（再开一个窗口）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: appsFixtures(pinned, { storage: running }),
    click: openDock,
    hover: '[role=menu] [class*="group/tile"] >> nth=1',
    wait: 2000,
    render: home,
  },
  {
    name: 'empty',
    title: 'Dock · 全部移出后的空 Dock',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: appsFixtures({ '/api/dock': { ok: true, dock: { order: [], pins: [] } } }),
    click: openDock,
    render: home,
  },
]);

export default designFrames;
