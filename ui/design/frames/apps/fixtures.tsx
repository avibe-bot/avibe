// The Apps frames' demo machine: a project folder the Files and Editor apps read (`/api/files/*`), a shell
// session the Terminal attaches to (its socket fixture), the Dock with pinned Show Pages, and window layouts
// the shipped window manager restores from Web Storage.
import { WindowLayer } from '@/components/apps/WindowLayer';
import { DockProvider } from '@/context/DockProvider';
import type { DockDoc } from '@/context/dockDoc';
import { WindowManagerProvider } from '@/context/WindowManagerProvider';
import { seedDefaultDock, showDockId } from '@/context/dockDoc';
import type { FsEntry } from '@/lib/filesApi';
import { WORKBENCH_WINDOWS_STORAGE_KEY, type PersistedWindow } from '@/lib/workbenchPersistence';

import { DESIGN_NOW } from '../../shell/clock';
import { demoProjects, demoSessions, inboxRow, type WorkbenchFixtures } from '../../shell/fixtures';
import type { DesignSocketMessage } from '../../shell/network';
import { agentsApi } from '../agents/fixtures';
import { showPages, showPagesApi } from '../show-pages/fixtures';
import { workbenchFixtures } from '../workbench/fixtures';

export const ROOT = '/Users/demo/avibe';
const HOME = '/Users/demo';
const secondsAgo = (minutes: number) => Math.round((DESIGN_NOW - minutes * 60_000) / 1000);

const dir = (name: string, minutesAgo: number): FsEntry => ({ name, kind: 'dir', size: null, mtime: secondsAgo(minutesAgo), ext: '' });
const file = (name: string, size: number, minutesAgo: number): FsEntry => ({
  name,
  kind: 'file',
  size,
  mtime: secondsAgo(minutesAgo),
  ext: name.includes('.') ? name.split('.').pop()! : '',
});

// Files hides dotfiles by default; the Editor's explorer shows them. This folder has none.
const listing = (path: string, entries: FsEntry[]) => {
  const body = { ok: true, path, parent: path.slice(0, path.lastIndexOf('/')) || '/', entries };
  return {
    [`/api/files/list?path=${encodeURIComponent(path)}&show_hidden=0`]: body,
    [`/api/files/list?path=${encodeURIComponent(path)}&show_hidden=1`]: body,
  };
};

/** A text file as `/api/files/content` serves it, with the metadata the Editor reads first. */
const text = (path: string, body: string, minutesAgo: number) => ({
  [`/api/files/content?path=${encodeURIComponent(path)}`]: new Response(body, { headers: { 'content-type': 'text/plain; charset=utf-8' } }),
  [`/api/files/meta?path=${encodeURIComponent(path)}`]: {
    ok: true,
    name: path.split('/').pop(),
    ext: path.split('.').pop(),
    kind: 'file',
    size: new TextEncoder().encode(body).length,
    mtime: secondsAgo(minutesAgo),
    mime: 'text/plain',
    text: true,
  },
});

export const APP_TSX = `import { useState } from 'react';

import { AppWindow } from './components/apps/AppWindow';

// 工作台的窗口外壳：文件、终端和编辑器都在这里打开。
export function App() {
  const [open, setOpen] = useState(false);
  return <AppWindow title="Files" open={open} onClose={() => setOpen(false)} />;
}
`;

const README = `# avibe

本地优先的 Agent OS：一条命令把这台机器变成 Agent 的运行环境。

## 开发

\`\`\`bash
cd ui && npm ci && npm run dev
\`\`\`
`;

/** Every Files and Editor read under the demo project. */
export const filesApi = {
  '/api/browse/favorites': {
    ok: true,
    favorites: [
      { key: 'home', path: HOME },
      { key: 'desktop', path: `${HOME}/Desktop` },
      { key: 'documents', path: `${HOME}/Documents` },
      { key: 'downloads', path: `${HOME}/Downloads` },
    ],
  },
  ...listing(ROOT, [
    dir('core', 60 * 3),
    dir('docs', 60 * 24 * 2),
    dir('tests', 60 * 5),
    dir('ui', 12),
    file('AGENTS.md', 12_480, 60 * 24),
    file('README.md', 4_912, 8),
    file('main.py', 2_204, 60 * 24 * 6),
    file('pyproject.toml', 3_350, 60 * 24 * 3),
  ]),
  ...listing(`${ROOT}/ui`, [
    dir('design', 30),
    dir('src', 12),
    file('index.html', 1_120, 60 * 24 * 9),
    file('package.json', 4_096, 60 * 24 * 2),
    file('vite.config.ts', 3_802, 60 * 24 * 4),
  ]),
  ...listing(`${ROOT}/ui/src`, [
    dir('components', 12),
    dir('context', 60),
    file('App.tsx', 412, 12),
    file('main.tsx', 640, 60 * 24 * 20),
    file('index.css', 48_210, 60 * 24),
  ]),
  ...text(`${ROOT}/ui/src/App.tsx`, APP_TSX, 12),
  ...text(`${ROOT}/README.md`, README, 8),
};

const encoder = (s: string) => s.replace(/\n/g, '\r\n');

/** A persistent shell that has run two commands, as the terminal socket streams it. */
export const terminalSocket: Record<string, DesignSocketMessage[]> = {
  '/api/terminal/': [
    JSON.stringify({ type: 'ready', persistent: true }),
    {
      binary: encoder(
        [
          '\x1b[32mdemo@macbook\x1b[0m \x1b[36m~/avibe\x1b[0m % git status',
          'On branch master',
          'nothing to commit, working tree clean',
          '',
          '\x1b[32mdemo@macbook\x1b[0m \x1b[36m~/avibe\x1b[0m % cd ui && npm run build',
          '',
          '> ui@0.0.0 build',
          '> tsc -b && vite build',
          '',
          '\x1b[32m✓\x1b[0m 2977 modules transformed.',
          '\x1b[32m✓ built in 1.94s\x1b[0m',
          '',
          '\x1b[32mdemo@macbook\x1b[0m \x1b[36m~/avibe/ui\x1b[0m % ',
        ].join('\n'),
      ),
    },
  ],
};

/** The Dock with two Show Pages pinned after the built-in apps. */
export const pinnedDock = (): DockDoc => {
  const base = seedDefaultDock();
  const pins = [showPages[0], showPages[1]].map((page) => ({
    session_id: page.session_id,
    title_snapshot: page.title ?? '',
    pinned_at: new Date(DESIGN_NOW - 60 * 60_000).toISOString(),
  }));
  return { order: [...base.order, ...pins.map((pin) => showDockId(pin.session_id))], pins };
};

/** A window as the window manager persists it; `z` stacks, the last listed is on top. */
export const win = (n: number, appId: string, bounds: PersistedWindow['bounds'], extra: Partial<PersistedWindow> = {}): PersistedWindow => ({
  id: `win-${n}`,
  appId,
  bounds,
  z: n,
  minimized: false,
  maximized: false,
  ...extra,
});

/** Web Storage holding these windows, which the shipped window manager restores on a desktop. */
export const windowLayout = (windows: PersistedWindow[]): Record<string, string> => ({
  [WORKBENCH_WINDOWS_STORAGE_KEY]: JSON.stringify({ version: 1, windows }),
});

// The project folder Files opens first is the most recently active project's.
const projects = demoProjects.map((project) =>
  project.id === 'proj_avibe' ? { ...project, last_active_at: new Date(DESIGN_NOW - 60_000).toISOString() } : project,
);

/** A running install whose apps read the demo machine, plus whatever a frame adds. */
export function appsFixtures(api: Record<string, unknown> = {}, extra: WorkbenchFixtures = {}): WorkbenchFixtures {
  return workbenchFixtures(
    { ...agentsApi(), ...showPagesApi(), ...filesApi, ...api },
    {
      projects,
      sessionsByProject: demoSessions,
      expandedProjects: ['proj_avibe'],
      inboxSessions: [inboxRow('ses_demo_cache', 'avibe', '修复中途断缓存', '定位到清理旧工具结果时前缀断开，PR 已开。', 12, 3)],
      unreadBySession: { ses_demo_cache: 3 },
      sockets: terminalSocket,
      ...extra,
    },
  );
}

/** One app window on its own, as the shipped window layer draws it from the saved layout (the frame's
 *  `storage`): the window manager and Dock AppShell provides, without the Workbench behind it. */
export const windowOnly = () => (
  <WindowManagerProvider>
    <DockProvider enabled>
      <div className="h-full w-full bg-background">
        <WindowLayer />
      </div>
    </DockProvider>
  </WindowManagerProvider>
);

/** A layout of one window inset 24 px in a frame of `width` × `height`. */
export const soloWindow = (appId: string, width: number, height: number, extra: Partial<PersistedWindow> = {}) =>
  windowLayout([win(1, appId, { x: 24, y: 24, width: width - 48, height: height - 48 }, extra)]);
