// The Terminal app: tabs over a shell session (xterm.js on the terminal socket), as a window and as the
// phone's full-screen route with its accessory key bar.
import { AppsTerminalPage } from '@/components/workbench/AppsTerminalPage';

import { frames } from '../../frame';
import { phoneShell } from '../workbench/fixtures';
import { appsFixtures, soloWindow, terminalSocket, windowOnly } from './fixtures';

const exited = {
  '/api/terminal/': [...terminalSocket['/api/terminal/'], JSON.stringify({ type: 'exit', code: 0 })],
};

const designFrames = frames([
  {
    name: 'window',
    title: '终端 · 窗口（持久会话）',
    width: 980,
    height: 640,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: soloWindow('terminal', 980, 640) }),
    wait: 2000,
    render: windowOnly,
  },
  {
    name: 'tabs',
    title: '终端 · 多个标签',
    width: 980,
    height: 640,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: soloWindow('terminal', 980, 640, { appState: { tabs: [{}, { title: 'vite dev' }, { title: 'pytest' }] } }) }),
    wait: 2000,
    render: windowOnly,
  },
  {
    name: 'exited',
    title: '终端 · Shell 已退出',
    width: 980,
    height: 640,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: soloWindow('terminal', 980, 640), sockets: exited }),
    wait: 2000,
    render: windowOnly,
  },
  {
    name: 'phone',
    title: '终端 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/apps/terminal',
    fixtures: appsFixtures(),
    wait: 2000,
    render: phoneShell('/apps/terminal', <AppsTerminalPage />),
  },
]);

export default designFrames;
