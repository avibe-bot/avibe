// The App Library's Apps tab: the built-in apps and the pinned Show Pages, each with its Dock switch, as a
// window and on the phone. Its Show Pages tab is drawn in `show-pages/show-pages`.
import { LibraryApp } from '@/apps/LibraryApp';

import { frames } from '../../frame';
import { phoneShell } from '../workbench/fixtures';
import { appsFixtures, pinnedDock, soloWindow, windowOnly } from './fixtures';

const pinned = { '/api/dock': { ok: true, dock: pinnedDock() } };

const designFrames = frames([
  {
    name: 'apps',
    title: '应用库 · 应用（内置与已固定的 Show Page）',
    width: 1000,
    height: 680,
    status: 'shipped',
    fixtures: appsFixtures(pinned, { storage: soloWindow('library', 1000, 680) }),
    wait: 800,
    render: windowOnly,
  },
  {
    name: 'apps-fresh',
    title: '应用库 · 应用（新安装，只有内置应用）',
    width: 1000,
    height: 560,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: soloWindow('library', 1000, 560) }),
    wait: 800,
    render: windowOnly,
  },
  {
    name: 'phone',
    title: '应用库 · 应用 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/apps/library',
    fixtures: appsFixtures(pinned),
    wait: 800,
    render: phoneShell('/apps/library', <LibraryApp />),
  },
]);

export default designFrames;
