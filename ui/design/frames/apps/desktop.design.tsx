// Windowed apps on the desktop Workbench: the shipped window manager restores a saved layout (Files and a
// Terminal, a maximized Editor), and the Apps launcher pins the Dock open beneath them.
import { Workbench } from '@/components/Workbench';

import { frames } from '../../frame';
import { phoneShell } from '../workbench/fixtures';
import { appsFixtures, ROOT, win, windowLayout } from './fixtures';

const home = phoneShell('/', <Workbench />);
const openDock = ['role=button[name="{t:apps.title}"]'];

const filesAndTerminal = windowLayout([
  win(1, 'files', { x: 340, y: 64, width: 860, height: 520 }),
  win(2, 'terminal', { x: 600, y: 330, width: 780, height: 470 }, { appState: { tabs: [{}, { title: 'vite' }] } }),
]);

const designFrames = frames([
  {
    name: 'windows',
    title: '应用 · 桌面窗口（文件与终端）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: appsFixtures({}, { storage: filesAndTerminal }),
    wait: 2000,
    render: home,
  },
  {
    name: 'windows-dock',
    title: '应用 · 桌面窗口与固定的 Dock',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: appsFixtures({}, { storage: filesAndTerminal }),
    click: openDock,
    wait: 2000,
    render: home,
  },
  {
    name: 'maximized',
    title: '应用 · 编辑器最大化（盖住侧边栏）',
    width: 1440,
    height: 900,
    status: 'shipped',
    route: '/',
    fixtures: appsFixtures(
      {},
      {
        storage: windowLayout([
          win(1, 'editor', { x: 0, y: 0, width: 1440, height: 900 }, {
            maximized: true,
            restoreBounds: { x: 360, y: 80, width: 1000, height: 640 },
            title: 'App.tsx',
            appState: { root: `${ROOT}/ui/src`, tabs: [{ path: `${ROOT}/ui/src/App.tsx`, name: 'App.tsx' }], activePath: `${ROOT}/ui/src/App.tsx` },
          }),
        ]),
      },
    ),
    wait: 2500,
    render: home,
  },
]);

export default designFrames;
