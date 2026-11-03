// The Files app: the whole machine as a Finder (favorites and projects, the folder, a status bar), as a
// window and as the phone's full-screen route. It opens on the most recently active project's folder.
import { AppsFileBrowserPage } from '@/components/workbench/AppsFileBrowserPage';

import { frames } from '../../frame';
import { phoneShell } from '../workbench/fixtures';
import { appsFixtures, ROOT, soloWindow, windowOnly } from './fixtures';

const rootList = `/api/files/list?path=${encodeURIComponent(ROOT)}&show_hidden=0`;
const layout = soloWindow('files', 1060, 660);

const designFrames = frames([
  {
    name: 'window',
    title: '文件 · 窗口（项目文件夹）',
    width: 1060,
    height: 660,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: layout }),
    wait: 1000,
    render: windowOnly,
  },
  {
    name: 'selected',
    title: '文件 · 选中一个文件',
    width: 1060,
    height: 660,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: layout }),
    wait: 1000,
    click: ['text=README.md'],
    render: windowOnly,
  },
  {
    name: 'empty',
    title: '文件 · 空文件夹',
    width: 1060,
    height: 520,
    status: 'shipped',
    fixtures: appsFixtures({ [rootList]: { ok: true, path: ROOT, parent: '/Users/demo', entries: [] } }, { storage: soloWindow('files', 1060, 520) }),
    wait: 1000,
    render: windowOnly,
  },
  {
    name: 'error',
    title: '文件 · 没有权限读取',
    width: 1060,
    height: 520,
    status: 'shipped',
    fixtures: appsFixtures(
      { [rootList]: { ok: false, error: { code: 'permission_denied', message: 'Permission denied' } } },
      { storage: soloWindow('files', 1060, 520) },
    ),
    wait: 1000,
    render: windowOnly,
  },
  {
    name: 'phone',
    title: '文件 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/apps/files',
    fixtures: appsFixtures(),
    wait: 1000,
    render: phoneShell('/apps/files', <AppsFileBrowserPage />),
  },
]);

export default designFrames;
