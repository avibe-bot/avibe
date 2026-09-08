/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import type { WorkbenchProject } from '../../context/ApiContext';
import { AppsFileBrowserPage } from './AppsFileBrowserPage';
import { FilePicker } from './FilePicker';

const state = vi.hoisted(() => ({
  projects: [] as WorkbenchProject[],
  listDir: vi.fn(),
}));

vi.mock('../../context/WorkbenchProjectsContext', () => ({
  useWorkbenchProjectsTree: () => ({ projects: state.projects }),
}));
vi.mock('../../context/WindowManagerContext', () => ({ useWindowManager: () => null }));
vi.mock('../../context/StandaloneAppTabContext', () => ({ useStandaloneAppTab: () => false }));
vi.mock('../../lib/useIsDesktop', () => ({ useIsDesktop: () => true, isDesktopViewport: () => true }));
vi.mock('../../lib/routeSurfaceActivity', () => ({ useRouteSurfaceWindowEvent: () => {} }));
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../../lib/filesApi', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../lib/filesApi')>(),
  listDir: state.listDir,
  systemFavorites: async () => [{ key: 'home', label: 'Home', path: '/tmp/home' }],
}));

function project(id: string, day: number): WorkbenchProject {
  return {
    id,
    scope_id: `avibe::project::${id}`,
    display_name: id,
    folder_path: `/tmp/${id}`,
    created_at: '2026-01-01T00:00:00Z',
    last_active_at: `2026-02-0${day}T00:00:00Z`,
    archived: false,
    capabilities: { can_chat: true, has_folder: true },
  };
}

beforeEach(() => {
  state.projects = [project('older', 1), project('recent', 2)];
  state.listDir.mockReset().mockImplementation(async (path: string) => ({ path, entries: [] }));
});
afterEach(cleanup);

describe.each(['browser', 'picker'] as const)('%s default folder', (surface) => {
  function mount() {
    return render(<MemoryRouter>{surface === 'browser'
      ? <AppsFileBrowserPage />
      : <FilePicker mode="open-directory" onCancel={() => {}} onConfirm={() => {}} />
    }</MemoryRouter>);
  }

  it.each([false, true])('opens the most recent project, independently of reversed navigation: %s', async (reversed) => {
    if (reversed) state.projects.reverse();
    mount();
    await waitFor(() => expect(state.listDir).toHaveBeenCalledWith('/tmp/recent', false));
    expect(state.listDir.mock.calls.every(([path]) => path === '/tmp/recent')).toBe(true);
  });

  it('retains the home fallback with no projects', async () => {
    state.projects = [];
    mount();
    await waitFor(() => expect(state.listDir).toHaveBeenCalledWith('/tmp/home', false));
  });
});

it('keeps an explicit picker path ahead of project defaults', async () => {
  render(<FilePicker mode="open-directory" initialPath="/tmp/explicit" onCancel={() => {}} onConfirm={() => {}} />);
  await waitFor(() => expect(state.listDir).toHaveBeenCalledWith('/tmp/explicit', false));
});
