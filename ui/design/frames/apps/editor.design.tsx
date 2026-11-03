// The Editor app (Monaco): the welcome page when nothing is open, a project with two files open, and the
// cross-file Search view, as a window; the phone's full-screen route.
import { AppsEditorPage } from '@/components/workbench/AppsEditorPage';
import { EDITOR_RECENTS_STORAGE_KEY } from '@/lib/editorRecents';

import { frames } from '../../frame';
import { phoneShell } from '../workbench/fixtures';
import { appsFixtures, ROOT, soloWindow, windowOnly } from './fixtures';

const SRC = `${ROOT}/ui/src`;
const project = {
  root: SRC,
  tabs: [
    { path: `${SRC}/App.tsx`, name: 'App.tsx' },
    { path: `${ROOT}/README.md`, name: 'README.md' },
  ],
  activePath: `${SRC}/App.tsx`,
};
const recents = {
  [EDITOR_RECENTS_STORAGE_KEY]: JSON.stringify({
    version: 1,
    folders: [ROOT, '/Users/demo/chainbot', '/Users/demo/ai-relay'],
    files: [
      { path: `${SRC}/App.tsx`, name: 'App.tsx' },
      { path: `${ROOT}/README.md`, name: 'README.md' },
    ],
  }),
};

const designFrames = frames([
  {
    name: 'welcome',
    title: '编辑器 · 欢迎页',
    width: 1100,
    height: 720,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: soloWindow('editor', 1100, 720) }),
    wait: 2000,
    render: windowOnly,
  },
  {
    name: 'welcome-recent',
    title: '编辑器 · 欢迎页（最近打开）',
    width: 1100,
    height: 720,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: { ...soloWindow('editor', 1100, 720), ...recents } }),
    wait: 2000,
    render: windowOnly,
  },
  {
    name: 'file',
    title: '编辑器 · 打开项目里的两个文件',
    width: 1100,
    height: 720,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: soloWindow('editor', 1100, 720, { title: 'App.tsx', appState: project }) }),
    wait: 2500,
    render: windowOnly,
  },
  {
    name: 'search',
    title: '编辑器 · 跨文件搜索',
    width: 1100,
    height: 720,
    status: 'shipped',
    fixtures: appsFixtures({}, { storage: soloWindow('editor', 1100, 720, { title: 'App.tsx', appState: project }) }),
    wait: 2500,
    click: ['role=button[name="{t:apps.editor.search.title}"]'],
    render: windowOnly,
  },
  {
    name: 'phone',
    title: '编辑器 · 手机',
    width: 390,
    height: 844,
    status: 'shipped',
    route: '/apps/editor',
    fixtures: appsFixtures({}, { storage: recents }),
    wait: 2000,
    render: phoneShell('/apps/editor', <AppsEditorPage />),
  },
]);

export default designFrames;

