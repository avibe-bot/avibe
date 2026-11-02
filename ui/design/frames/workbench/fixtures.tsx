// What the Workbench pages share: the reads the shipped AppShell makes on every route, and two ways to
// draw a page — its desktop content column (beside the sidebar, which has its own frames), or a phone
// screen inside the shipped AppShell with its header and tab bar.
import type { ReactNode } from 'react';
import { Route, Routes } from 'react-router-dom';

import { AppShell } from '@/components/AppShell';
import { seedDefaultDock } from '@/context/dockDoc';

import { demoBots, demoProjects, type WorkbenchFixtures } from '../../shell/fixtures';
import { running, settingsConfig } from '../settings/fixtures';
import { consoleContentClass, consolePageClass } from '../skills/fixtures';

/** The reads AppShell and its sidebar make on any route. */
export const shellApi = {
  '/api/config': settingsConfig,
  '/api/version': { current: '3.2.4', latest: '3.2.4', has_update: false, error: null },
  '/api/session': { remote: false },
  // A fresh install's Dock: every built-in app docked, no Show Page pinned.
  '/api/dock': { ok: true, dock: seedDefaultDock() },
  '/api/macos/login-session': { ok: true, state: 'healthy' },
  // The Show Pages inventory the search and the Library read; a page frame overrides it.
  '/api/show-pages': { pages: [] },
  '/api/workbench/prefs': { ok: true, background_work_banner_enabled: true, fast_mode_default: false },
  // The composer's voice input and the desktop-only Computer Use line, as a browser install answers them.
  '/api/asr/status': { available: false },
  '/api/desktop/computer-use/status': { supported: false },
};

/** A running install with the demo projects and Bots, plus whatever the page reads. */
export function workbenchFixtures(api: Record<string, unknown> = {}, extra: WorkbenchFixtures = {}): WorkbenchFixtures {
  const projects = extra.projects ?? demoProjects;
  return {
    status: running,
    projects,
    bots: demoBots,
    ...extra,
    api: { ...shellApi, '/api/projects': { ok: true, projects }, ...api },
  };
}

/** A page in AppShell's desktop content column: the console glow behind the padded column. */
export const consolePage = (page: ReactNode) => () => (
  <div className={consolePageClass}>
    <div className={consoleContentClass}>{page}</div>
  </div>
);

/** A page as a phone shows it: inside the shipped AppShell, with its header and tab bar. */
export const phoneShell = (path: string, page: ReactNode) => () => (
  <Routes>
    <Route element={<AppShell />}>
      <Route path={path} element={page} />
    </Route>
  </Routes>
);
