/* @vitest-environment jsdom */

import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import type { FsListing } from '../../lib/filesApi';

const projects = vi.hoisted(() => ({ value: [] as unknown[] | null, error: null as string | null }));
const listDir = vi.hoisted(() =>
  vi.fn(async (path: string): Promise<FsListing> => ({
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  projects.value = [];
  projects.error = null;
  systemFavorites.mockReset().mockResolvedValue([{ key: 'home', path: '/workspace' }]);
  resolveDirectoryPath.mockImplementation(async (path: string) => path);
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

it.each(['home', 'project'] as const)('canonicalizes the symlinked %s fallback', async (source) => {
  const canonicalPath = '/real/项目';
  if (source === 'home') {
    systemFavorites.mockResolvedValue([{ key: 'home', path: '/folder-link' }]);
  } else {
    projects.value = [{ id: 'project', display_name: 'Project', folder_path: '/folder-link' }];
  }
  resolveDirectoryPath.mockResolvedValue(canonicalPath);
  const onSelect = vi.fn();
  render(<FolderBrowser onSelect={onSelect} onClose={() => {}} />);

  await waitFor(() => expect(listDir).toHaveBeenCalledWith(canonicalPath, false));
  expect(listDir).not.toHaveBeenCalledWith('/folder-link', expect.anything());
  fireEvent.click(screen.getByRole('button', { name: 'directoryBrowser.select' }));
  expect(onSelect).toHaveBeenCalledWith(canonicalPath);
});

it.each(['favorite', 'project'] as const)('canonicalizes a selected %s before listing', async (source) => {
  const canonicalPath = '/real/项目';
  if (source === 'favorite') {
    systemFavorites.mockResolvedValue([{ key: 'home', path: '/folder-link' }]);
  } else {
    projects.value = [{ id: 'project', display_name: 'folder-link', folder_path: '/folder-link' }];
  }
  resolveDirectoryPath.mockImplementation(async (path: string) => path === '/folder-link' ? canonicalPath : path);
  const onSelect = vi.fn();
  render(<FolderBrowser initialPath="/workspace" onSelect={onSelect} onClose={() => {}} />);
  await screen.findByText('src');

  fireEvent.click(screen.getAllByRole('button', { name: 'folder-link' })[0]);
  await waitFor(() => expect(listDir).toHaveBeenLastCalledWith(canonicalPath, false));
  expect(listDir).not.toHaveBeenCalledWith('/folder-link', expect.anything());
  fireEvent.click(screen.getByRole('button', { name: 'directoryBrowser.select' }));
  expect(onSelect).toHaveBeenCalledWith(canonicalPath);
});

it('waits for favorite canonicalization before refreshing with the new hidden-file state', async () => {
  const resolved = deferred<string>();
  systemFavorites.mockResolvedValue([{ key: 'home', path: '/folder-link' }]);
  resolveDirectoryPath.mockImplementation((path: string) => path === '/folder-link' ? resolved.promise : Promise.resolve(path));
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);
  await screen.findByText('src');

  fireEvent.click(screen.getAllByRole('button', { name: 'folder-link' })[0]);
  await waitFor(() => expect(resolveDirectoryPath).toHaveBeenCalledWith('/folder-link'));
  fireEvent.click(screen.getByRole('checkbox'));
  expect(listDir).not.toHaveBeenCalledWith('/folder-link', true);

  await act(async () => resolved.resolve('/real/项目'));
  await waitFor(() => expect(listDir).toHaveBeenLastCalledWith('/real/项目', true));
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

it('resolves absolute configured paths before using the Files API', async () => {
  resolveDirectoryPath.mockResolvedValueOnce('/real/workspace');
  render(<FolderBrowser initialPath="/workspace-link" onSelect={() => {}} onClose={() => {}} />);

  await waitFor(() => expect(resolveDirectoryPath).toHaveBeenCalledWith('/workspace-link'));
  await waitFor(() => expect(listDir).toHaveBeenCalledWith('/real/workspace', false));
});

it('navigates to an arbitrary path entered in the picker', async () => {
  resolveDirectoryPath.mockImplementation(async (path: string) => (path === '\\\\server\\share' ? '/server/share' : path));
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await screen.findByText('src');
  fireEvent.click(screen.getByRole('button', { name: 'directoryBrowser.editPath' }));
  const input = screen.getByRole('textbox', { name: 'directoryBrowser.editPath' });
  fireEvent.change(input, { target: { value: '\\\\server\\share' } });
  fireEvent.keyDown(input, { key: 'Enter' });

  await waitFor(() => expect(resolveDirectoryPath).toHaveBeenCalledWith('\\\\server\\share'));
  await waitFor(() => expect(listDir).toHaveBeenLastCalledWith('/server/share', false));
});

it('discards a manual path submission after explicit navigation', async () => {
  let resolvePath!: (path: string) => void;
  resolveDirectoryPath.mockImplementation((path: string) => {
    if (path === '/stale') {
      return new Promise((resolve) => {
        resolvePath = resolve;
      });
    }
    return Promise.resolve(path);
  });
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await screen.findByText('src');
  fireEvent.click(screen.getByRole('button', { name: 'directoryBrowser.editPath' }));
  const input = screen.getByRole('textbox', { name: 'directoryBrowser.editPath' });
  fireEvent.change(input, { target: { value: '/stale' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(resolveDirectoryPath).toHaveBeenCalledWith('/stale'));

  fireEvent.click((await screen.findAllByRole('button', { name: 'workspace' }))[0]);
  await waitFor(() => expect(listDir).toHaveBeenLastCalledWith('/workspace', false));
  resolvePath('/stale');

  await waitFor(() => expect(listDir).not.toHaveBeenCalledWith('/stale', false));
});

it('keeps only the newest manual path submission', async () => {
  let resolveFirst!: (path: string) => void;
  let resolveSecond!: (path: string) => void;
  resolveDirectoryPath.mockImplementation((path: string) => {
    if (path === '/first') {
      return new Promise((resolve) => {
        resolveFirst = resolve;
      });
    }
    if (path === '/second') {
      return new Promise((resolve) => {
        resolveSecond = resolve;
      });
    }
    return Promise.resolve(path);
  });
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);

  await screen.findByText('src');
  fireEvent.click(screen.getByRole('button', { name: 'directoryBrowser.editPath' }));
  const input = screen.getByRole('textbox', { name: 'directoryBrowser.editPath' });
  fireEvent.change(input, { target: { value: '/first' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(resolveDirectoryPath).toHaveBeenCalledWith('/first'));

  fireEvent.change(input, { target: { value: '/second' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  await waitFor(() => expect(resolveDirectoryPath).toHaveBeenCalledWith('/second'));

  resolveFirst('/first');
  await waitFor(() => expect(listDir).not.toHaveBeenCalledWith('/first', false));
  resolveSecond('/second');
  await waitFor(() => expect(listDir).toHaveBeenLastCalledWith('/second', false));
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

it('ignores an initial path resolution error after explicit navigation', async () => {
  let rejectPath!: (cause: Error) => void;
  resolveDirectoryPath.mockReturnValueOnce(
    new Promise((_resolve, reject) => {
      rejectPath = reject;
    }),
  );
  const onSelect = vi.fn();
  render(<FolderBrowser initialPath="./workspace" onSelect={onSelect} onClose={() => {}} />);

  await waitFor(() => expect(resolveDirectoryPath).toHaveBeenCalledWith('./workspace'));
  fireEvent.click((await screen.findAllByRole('button', { name: 'workspace' }))[0]);
  await waitFor(() => expect(listDir).toHaveBeenCalledWith('/workspace', false));

  await act(async () => {
    rejectPath(new Error('stale initial path'));
    await Promise.resolve();
  });

  expect(screen.queryByText('apps.fileBrowser.errors.listFailed')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'directoryBrowser.select' }));
  expect(onSelect).toHaveBeenCalledWith('/workspace');
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

it('restores path editor focus after the route surface resumes', async () => {
  const props = { initialPath: '/workspace', onSelect: () => {}, onClose: () => {} };
  const view = render(
    <RouteSurfaceActiveContext.Provider value>
      <FolderBrowser {...props} />
    </RouteSurfaceActiveContext.Provider>,
  );

  await screen.findByText('src');
  fireEvent.click(screen.getByRole('button', { name: 'directoryBrowser.editPath' }));
  const input = screen.getByRole('textbox', { name: 'directoryBrowser.editPath' });
  fireEvent.change(input, { target: { value: '/draft' } });

  view.rerender(
    <RouteSurfaceActiveContext.Provider value={false}>
      <FolderBrowser {...props} />
    </RouteSurfaceActiveContext.Provider>,
  );
  expect(screen.queryByRole('textbox', { name: 'directoryBrowser.editPath' })).toBeNull();

  view.rerender(
    <RouteSurfaceActiveContext.Provider value>
      <FolderBrowser {...props} />
    </RouteSurfaceActiveContext.Provider>,
  );
  const resumedInput = await screen.findByRole('textbox', { name: 'directoryBrowser.editPath' });
  expect((resumedInput as HTMLInputElement).value).toBe('/draft');
  await waitFor(() => expect(document.activeElement).toBe(resumedInput));
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

it.each(['hidden-files toggle', 'refresh'] as const)('preserves search across repeated %s with listings pending', async (action) => {
  searchNames.mockResolvedValue({ results: [], truncated: false });
  render(<FolderBrowser initialPath="/workspace" onSelect={() => {}} onClose={() => {}} />);
  await screen.findByText('src');
  const search = screen.getByPlaceholderText('apps.fileBrowser.searchPlaceholder') as HTMLInputElement;
  fireEvent.change(search, { target: { value: 'src' } });
  await waitFor(() => expect(searchNames).toHaveBeenCalledTimes(1));
  const first = deferred<FsListing>();
  const second = deferred<FsListing>();
  listDir.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  const trigger = action === 'refresh'
    ? screen.getByRole('button', { name: 'apps.fileBrowser.refresh' })
    : screen.getByRole('checkbox');
  fireEvent.click(trigger);
  await waitFor(() => expect(listDir).toHaveBeenCalledTimes(2));
  fireEvent.click(trigger);
  await waitFor(() => expect(listDir).toHaveBeenCalledTimes(3));

  expect(search.value).toBe('src');
  await waitFor(() => expect(searchNames).toHaveBeenLastCalledWith('/workspace', 'src', false, expect.any(AbortSignal)));
  await act(async () => second.resolve({
    ok: true, path: '/workspace', parent: '/',
    entries: [{ name: 'fresh-dir', kind: 'dir', size: null, mtime: 1, ext: '' }],
  }));
  await act(async () => first.reject(new Error('stale listing failed')));
  expect(search.value).toBe('src');
  expect(screen.queryByText('apps.fileBrowser.errors.listFailed')).toBeNull();
  fireEvent.change(search, { target: { value: '' } });
  expect(await screen.findByText('fresh-dir')).toBeTruthy();
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
