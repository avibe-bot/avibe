/* @vitest-environment jsdom */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

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

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock('../../context/WorkbenchProjectsContext', () => ({ useWorkbenchProjectsTree: () => ({ projects: [] }) }));
vi.mock('../../lib/useIsDesktop', () => ({ useIsDesktop: () => true }));
vi.mock('../../lib/filesApi', () => ({
  fileBrowserErrorMessage: (_error: unknown, _t: unknown, fallback: string) => fallback,
  isPlainEntryName: (value: string) => value.trim() !== '',
  joinPath: (base: string, name: string) => `${base}/${name}`,
  listDir,
  makeDir: vi.fn(),
  pathCrumbs: (path: string) => [{ label: '/', path: '/' }, { label: path.slice(1), path }],
  searchNames: vi.fn(),
  systemFavorites: vi.fn().mockResolvedValue([{ key: 'home', path: '/workspace' }]),
}));

import { FolderBrowser } from './folder-browser';

afterEach(() => {
  vi.clearAllMocks();
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
