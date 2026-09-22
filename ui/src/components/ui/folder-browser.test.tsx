/* @vitest-environment jsdom */

import { StrictMode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';

const projects = vi.hoisted(() => ({ value: [] as unknown[] | null, error: null as string | null }));
const listDir = vi.hoisted(() =>
  vi.fn(async (path: string) => ({
    ok: true as const,
    path,
    parent: path === '/workspace' ? '/' : '/workspace',
    entries: path === '/workspace'
      ? [
          { name: 'src', kind: 'dir' as const, size: null, mtime: 2, ext: '' },
          { name: 'README.md', kind: 'file' as const, size: 12, mtime: 1, ext: 'md' },
        ]
      : [],
  })),
);
const makeDir = vi.hoisted(() => vi.fn());
const searchNames = vi.hoisted(() => vi.fn());
const systemFavorites = vi.hoisted(() => vi.fn().mockResolvedValue([{ key: 'home', path: '/workspace' }]));
const resolveDirectoryPath = vi.hoisted(() => vi.fn());

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../../context/WorkbenchProjectsContext', () => ({
  useWorkbenchProjectsTree: () => ({ projects: projects.value, projectsError: projects.error }),
}));
vi.mock('../../lib/useIsDesktop', () => ({ useIsDesktop: () => true }));
vi.mock('../../lib/filesApi', () => ({
  fileBrowserErrorMessage: (_error: unknown, _t: unknown, fallback: string) => fallback,
  isPlainEntryName: (value: string) => value.trim() !== '',
  joinPath: (base: string, name: string) => `${base}/${name}`,
  listDir,
  makeDir,
  pathCrumbs: (path: string) => [{ label: '/', path: '/' }, { label: path.slice(1), path }],
  resolveDirectoryPath,
  searchNames,
  systemFavorites,
}));

import { FolderBrowser } from './folder-browser';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  projects.value = [];
  projects.error = null;
  listDir.mockReset().mockImplementation(async (path: string) => ({
    ok: true as const,
    path,
    parent: path === '/workspace' ? '/' : '/workspace',
    entries: path === '/workspace'
      ? [
          { name: 'src', kind: 'dir' as const, size: null, mtime: 2, ext: '' },
          { name: 'README.md', kind: 'file' as const, size: 12, mtime: 1, ext: 'md' },
        ]
      : [],
  }));
});

it('reuses the Files surface while keeping folder selection actions focused', async () => {
  const onSelect = vi.fn();
  render(<FolderBrowser initialPath="/workspace" onSelect={onSelect} onClose={() => {}} />);

  expect(await screen.findByText('src')).toBeTruthy();
  expect(screen.getByText('apps.fileBrowser.newFolder')).toBeTruthy();
  expect(screen.queryByText('apps.fileBrowser.newFile')).toBeNull();
  expect(screen.queryByText('apps.fileBrowser.upload')).toBeNull();

  fireEvent.click(screen.getByText('src'));
  await waitFor(() => expect(listDir).toHaveBeenLastCalledWith('/workspace/src', false));
  fireEvent.click(screen.getByRole('button', { name: 'directoryBrowser.select' }));
  expect(onSelect).toHaveBeenCalledWith('/workspace/src');
});

it('uses the shared dialog close behavior', async () => {
  const onClose = vi.fn();
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={onClose} />);

  await screen.findByText('src');
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });

  expect(onClose).toHaveBeenCalledOnce();
});

it('completes initial navigation after a StrictMode effect replay', async () => {
  render(
    <StrictMode>
      <FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />
    </StrictMode>,
  );

  await waitFor(() => expect(listDir).toHaveBeenCalledWith('/workspace', false));
});

it('waits for the project tree before selecting the initial project', async () => {
  projects.value = null;
  const view = render(<FolderBrowser onSelect={() => {}} onClose={() => {}} />);

  await waitFor(() => expect(listDir).not.toHaveBeenCalled());

  projects.value = [
    {
      id: 'project-1',
      display_name: 'Project',
      folder_path: '/project',
      created_at: '2026-09-22T00:00:00Z',
      last_active_at: '2026-09-22T00:00:00Z',
    },
  ];
  view.rerender(<FolderBrowser onSelect={() => {}} onClose={() => {}} />);

  await waitFor(() => expect(listDir).toHaveBeenCalledWith('/project', false));
});

it('falls back to home when system favorites fail during initial navigation', async () => {
  systemFavorites.mockRejectedValueOnce(new Error('offline'));
  render(<FolderBrowser onSelect={() => {}} onClose={() => {}} />);

  await waitFor(() => expect(listDir).toHaveBeenCalledWith('~', false));
});

it('resolves relative configured paths before using the Files API', async () => {
  let resolvePath!: (path: string) => void;
  resolveDirectoryPath.mockReturnValueOnce(
    new Promise((resolve) => {
      resolvePath = resolve;
    }),
  );
  render(<FolderBrowser initialPath='./workspace' onSelect={() => {}} onClose={() => {}} />);

  await waitFor(() => expect(resolveDirectoryPath).toHaveBeenCalledWith('./workspace'));
  resolvePath('/resolved/workspace');
  await waitFor(() => expect(listDir).toHaveBeenCalledWith('/resolved/workspace', false));
});

it('does not override explicit navigation when initial path resolution finishes later', async () => {
  let resolvePath!: (path: string) => void;
  resolveDirectoryPath.mockReturnValueOnce(
    new Promise((resolve) => {
      resolvePath = resolve;
    }),
  );
  render(<FolderBrowser initialPath="./workspace" onSelect={() => {}} onClose={() => {}} />);

  await waitFor(() => expect(resolveDirectoryPath).toHaveBeenCalledWith('./workspace'));
  fireEvent.click((await screen.findAllByRole('button', { name: 'workspace' }))[0]);
  await waitFor(() => expect(listDir).toHaveBeenCalledWith('/workspace', false));

  resolvePath('/resolved/workspace');
  await waitFor(() => expect(listDir).not.toHaveBeenCalledWith('/resolved/workspace', false));
});

it('refreshes a pending destination when hidden files change', async () => {
  const requests: Array<{
    path: string;
    showHidden: boolean;
    resolve: (result: {
      ok: true;
      path: string;
      parent: string;
      entries: Array<{ name: string; kind: 'dir'; size: null; mtime: number; ext: string }>;
    }) => void;
  }> = [];
  listDir.mockImplementation((path: string, showHidden: boolean) => new Promise((resolve) => {
    requests.push({ path, showHidden, resolve });
  }));
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await waitFor(() => expect(requests).toHaveLength(1));
  requests[0].resolve({ ok: true, path: '/workspace', parent: '/', entries: [{ name: 'src', kind: 'dir', size: null, mtime: 1, ext: '' }] });
  await screen.findByText('src');

  fireEvent.click(screen.getByText('src'));
  await waitFor(() => expect(requests.some(({ path, showHidden }) => path === '/workspace/src' && !showHidden)).toBe(true));
  fireEvent.click(screen.getByRole('checkbox'));
  await waitFor(() => expect(requests.some(({ path, showHidden }) => path === '/workspace/src' && showHidden)).toBe(true));
});

it('falls back when the project tree fails to load', async () => {
  projects.value = null;
  projects.error = 'project tree unavailable';
  render(<FolderBrowser onSelect={() => {}} onClose={() => {}} />);

  await waitFor(() => expect(listDir).toHaveBeenCalledWith('/workspace', false));
});

it('keeps a new-folder draft while the route surface is suspended', async () => {
  const props = { initialPath: '/workspace', onSelect: () => {}, onClose: () => {} };
  const view = render(
    <RouteSurfaceActiveContext.Provider value>
      <FolderBrowser {...props} />
    </RouteSurfaceActiveContext.Provider>,
  );

  await screen.findByText('src');
  fireEvent.click(screen.getByRole('button', { name: 'apps.fileBrowser.newFolder' }));
  const input = screen.getByPlaceholderText('apps.fileBrowser.newFolderPlaceholder') as HTMLInputElement;
  fireEvent.change(input, { target: { value: 'draft-folder' } });

  view.rerender(
    <RouteSurfaceActiveContext.Provider value={false}>
      <FolderBrowser {...props} />
    </RouteSurfaceActiveContext.Provider>,
  );
  expect(screen.queryByPlaceholderText('apps.fileBrowser.newFolderPlaceholder')).toBeNull();

  view.rerender(
    <RouteSurfaceActiveContext.Provider value>
      <FolderBrowser {...props} />
    </RouteSurfaceActiveContext.Provider>,
  );
  expect((await screen.findByPlaceholderText('apps.fileBrowser.newFolderPlaceholder') as HTMLInputElement).value).toBe('draft-folder');
});

it('re-fetches the initial listing when hidden files are toggled while loading', async () => {
  let resolveInitial!: (result: { ok: true; path: string; parent: string; entries: never[] }) => void;
  listDir.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        resolveInitial = resolve;
      }),
  );
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await waitFor(() => expect(listDir).toHaveBeenCalledWith('/workspace', false));
  fireEvent.click(screen.getByRole('checkbox'));
  resolveInitial({ ok: true, path: '/workspace', parent: '/', entries: [] });

  await waitFor(() => expect(listDir).toHaveBeenLastCalledWith('/workspace', true));
});

it('preserves the active search when refreshing', async () => {
  searchNames.mockResolvedValue({ results: [], truncated: false });
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await screen.findByText('src');
  const search = screen.getByRole('textbox');
  fireEvent.change(search, { target: { value: 'src' } });
  await waitFor(() => expect(searchNames).toHaveBeenCalledWith('/workspace', 'src', false, expect.any(AbortSignal)));

  fireEvent.click(screen.getByRole('button', { name: 'apps.fileBrowser.refresh' }));
  expect((search as HTMLInputElement).value).toBe('src');
  await waitFor(() => expect(searchNames).toHaveBeenCalledTimes(2));
});

it('refreshes the directory listing when hidden files change during search', async () => {
  searchNames.mockResolvedValue({ results: [], truncated: false });
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await screen.findByText('src');
  const search = screen.getByRole('textbox');
  fireEvent.change(search, { target: { value: 'src' } });
  await waitFor(() => expect(searchNames).toHaveBeenCalledWith('/workspace', 'src', false, expect.any(AbortSignal)));

  fireEvent.click(screen.getByRole('checkbox'));
  expect((search as HTMLInputElement).value).toBe('src');
  await waitFor(() => expect(listDir).toHaveBeenLastCalledWith('/workspace', true));
});

it('does not show a false empty state while search is pending', async () => {
  let resolveSearch!: (result: { results: []; truncated: boolean }) => void;
  searchNames.mockReturnValueOnce(
    new Promise<{ results: []; truncated: boolean }>((resolve) => {
      resolveSearch = resolve;
    }),
  );
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await screen.findByText('src');
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'src' } });
  await waitFor(() => expect(searchNames).toHaveBeenCalledWith('/workspace', 'src', false, expect.any(AbortSignal)));

  expect(screen.queryByText('apps.fileBrowser.noMatches')).toBeNull();
  resolveSearch({ results: [], truncated: false });
  expect(await screen.findByText('apps.fileBrowser.noMatches')).toBeTruthy();
});

it('cancels folder creation when search starts', async () => {
  const onClose = vi.fn();
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={onClose} />);

  await screen.findByText('src');
  fireEvent.click(screen.getByRole('button', { name: 'apps.fileBrowser.newFolder' }));
  expect(screen.getByPlaceholderText('apps.fileBrowser.newFolderPlaceholder')).toBeTruthy();

  fireEvent.change(screen.getByPlaceholderText('apps.fileBrowser.searchPlaceholder'), { target: { value: 'src' } });
  expect(screen.queryByPlaceholderText('apps.fileBrowser.newFolderPlaceholder')).toBeNull();

  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
  expect(onClose).toHaveBeenCalledOnce();
});

it('keeps selection enabled after a new-folder operation fails', async () => {
  makeDir.mockRejectedValueOnce(new Error('already exists'));
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await screen.findByText('src');
  const select = screen.getByRole('button', { name: 'directoryBrowser.select' });
  fireEvent.click(screen.getByRole('button', { name: 'apps.fileBrowser.newFolder' }));
  const input = screen.getByPlaceholderText('apps.fileBrowser.newFolderPlaceholder');
  fireEvent.change(input, { target: { value: 'existing' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  await screen.findByText('apps.fileBrowser.errors.createFolderFailed');
  expect((select as HTMLButtonElement).disabled).toBe(false);

  fireEvent.keyDown(input, { key: 'Escape' });
  expect(screen.queryByPlaceholderText('apps.fileBrowser.newFolderPlaceholder')).toBeNull();
});

it('reports truncated directory listings', async () => {
  listDir.mockResolvedValueOnce({
    ok: true as const,
    path: '/workspace',
    parent: '/',
    entries: [{ name: 'src', kind: 'dir' as const, size: null, mtime: 2, ext: '' }],
    truncated: true,
    limit: 1,
  });
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await screen.findByText('src');
  expect(screen.getByText(/apps\.fileBrowser\.listTruncated/)).toBeTruthy();
});
