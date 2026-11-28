/* @vitest-environment jsdom */

// Settings › Experimental (ui/design: settings/source-filter/*): the renamed experiment
// keeps its existing route, API and hook directory. Real catalogs guard the displayed name;
// the former key-only translation stub could not catch a stale name in either language.
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { SettingsExperimentalPage, SettingsSourceFilterPage } from './SettingsExperimentalPages';

const { apiFetch, launchApp } = vi.hoisted(() => ({ apiFetch: vi.fn(), launchApp: vi.fn() }));

vi.mock('@/apps/useAppLaunch', () => ({ useAppLaunch: () => launchApp }));

vi.mock('@/lib/apiFetch', () => ({ apiFetch }));
// Isolate both native and fallback clipboard paths at their shared entry point.
vi.mock('@/lib/utils', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/lib/utils')>(),
  copyTextToClipboard: vi.fn(async () => true),
}));

const hook = (found: boolean) => ({ found, last_failure: null });
const status = (enabled: boolean) => ({
  ok: true,
  enabled,
  dir: '/home/demo/.avibe/hooks/source_filter',
  hooks: {
    before_search: hook(true), after_search: hook(false), before_fetch: hook(false), after_fetch: hook(false),
    after_read: hook(false), after_bash: hook(false),
  },
});
const answer = (body: unknown, code = 200) => new Response(JSON.stringify(body), { status: code });

async function page(lng: string, detail = false) {
  const i18n = createInstance();
  await i18n.init({ lng, resources: { en: { translation: en }, zh: { translation: zh } }, interpolation: { escapeValue: false } });
  return render(
    <I18nextProvider i18n={i18n}>
      <MemoryRouter>{detail ? <SettingsSourceFilterPage /> : <SettingsExperimentalPage />}</MemoryRouter>
    </I18nextProvider>,
  );
}

afterEach(() => {
  cleanup();
  apiFetch.mockReset();
  launchApp.mockReset();
});

describe.each([
  { lng: 'en', title: 'Information cocoon', use: 'Use information cocoon', on: 'On', missing: 'Not Found' },
  { lng: 'zh', title: '信息茧房', use: '启用信息茧房', on: '已开启', missing: '未找到' },
])('Settings › Experimental ($lng)', ({ lng, title, use, on, missing }) => {
  it('lists the renamed experiment without changing its route or adding a switch', async () => {
    apiFetch.mockResolvedValue(answer(status(true)));
    await page(lng);
    const row = await screen.findByRole('link', { name: new RegExp(title) });
    expect(row.getAttribute('href')).toBe('/settings/experimental/source-filter');
    await screen.findByText(on);
    expect(screen.queryByRole('switch')).toBeNull();
  });

  it('uses the new title and switch label while keeping the API and script directory', async () => {
    apiFetch.mockResolvedValueOnce(answer(status(false))).mockResolvedValueOnce(answer(status(true)));
    await page(lng, true);
    await screen.findByRole('heading', { name: title });
    await screen.findByText(use);
    const toggle = await screen.findByRole('switch');
    await waitFor(() => expect((toggle as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByText('before_search.py')).toBeNull();
    fireEvent.click(toggle);
    await screen.findByText('before_search.py');
    await screen.findByText('/home/demo/.avibe/hooks/source_filter');
    const [path, init] = apiFetch.mock.calls[1];
    expect(path).toBe('/api/settings/experimental/source-filter');
    expect(init).toMatchObject({ method: 'PUT', body: JSON.stringify({ enabled: true }) });
    await screen.findByText('after_read.py');
    await screen.findByText('after_bash.py');
    expect(screen.queryByText('before_read.py')).toBeNull();
    expect(screen.queryByText('before_bash.py')).toBeNull();
    expect(screen.getAllByText(missing)).toHaveLength(5);
  });

  // The settings boundary owns target construction and non-mutating navigation. Launcher tests
  // cover platform dispatch; this catches a missing/failed row becoming disabled or using HOME.
  it('opens all six script names, including missing and failed hooks, without changing files or the switch', async () => {
    const current = status(true);
    const failed = { found: true, last_failure: { at: '2026-11-28T12:00:00Z', reason: 'fixture failure' } };
    const names = ['before_search', 'after_search', 'before_fetch', 'after_fetch', 'after_read', 'after_bash'];
    apiFetch.mockImplementation(async (path: string) => {
      if (path === '/api/settings/experimental/source-filter') {
        return answer({ ...current, hooks: { ...current.hooks, after_bash: failed } });
      }
      const filename = new URL(path, 'http://fixture').searchParams.get('path')!.split('/').pop();
      return filename === 'before_search.py' || filename === 'after_bash.py'
        ? answer({ ok: true, mtime: 42 })
        : answer({ ok: false, error: { code: 'not_found' } }, 404);
    });
    await page(lng, true);
    for (const [index, name] of names.entries()) {
      fireEvent.click(await screen.findByRole('button', { name: `${name}.py`, exact: true }));
      await waitFor(() => expect(launchApp).toHaveBeenCalledTimes(index + 1));
      expect(launchApp).toHaveBeenLastCalledWith({
        appId: 'editor', title: `${name}.py`,
        params: {
          path: `/home/demo/.avibe/hooks/source_filter/${name}.py`, filename: `${name}.py`,
          rootDir: '/home/demo/.avibe/hooks/source_filter', mtime: index === 0 || index === 5 ? 42 : null,
        },
      });
    }
    expect(apiFetch.mock.calls).toHaveLength(7);
    expect(apiFetch.mock.calls.every(([path, init]) =>
      !init?.method && (path === '/api/settings/experimental/source-filter' || path.startsWith('/api/files/meta?')),
    )).toBe(true);
  });

  it('reports a metadata failure instead of silently opening without save-conflict protection', async () => {
    apiFetch.mockResolvedValueOnce(answer(status(true))).mockResolvedValueOnce(answer({
      ok: false, error: { code: 'permission_denied', message: 'raw failure' },
    }, 403));
    await page(lng, true);
    fireEvent.click(await screen.findByRole('button', { name: 'before_search.py', exact: true }));
    expect((await screen.findByRole('alert')).textContent).toBe((lng === 'en' ? en : zh).apps.fileBrowser.errors.permission_denied);
    expect(launchApp).not.toHaveBeenCalled();
  });
});

it.each([
  ['/custom/中文 hooks/source_filter/', '/custom/中文 hooks/source_filter/after_read.py'],
  ['C:\\Users\\demo\\.avibe\\hooks\\source_filter', 'C:\\Users\\demo\\.avibe\\hooks\\source_filter\\after_read.py'],
  ['\\\\server\\share\\hooks', '\\\\server\\share\\hooks\\after_read.py'],
])('uses the API directory for %s', async (dir, path) => {
  apiFetch.mockResolvedValueOnce(answer({ ...status(true), dir })).mockResolvedValueOnce(answer({ ok: true, mtime: 9 }));
  await page('en', true);
  fireEvent.click(await screen.findByRole('button', { name: 'after_read.py', exact: true }));
  await waitFor(() => expect(launchApp).toHaveBeenCalledWith({
    appId: 'editor', title: 'after_read.py', params: { path, filename: 'after_read.py', rootDir: dir, mtime: 9 },
  }));
  expect(apiFetch.mock.calls[1][0]).toBe(`/api/files/meta?path=${encodeURIComponent(path)}`);
});

it('ignores an older click and a metadata read finishing after the page closes', async () => {
  let finishFirst!: (response: Response) => void;
  let finishLast!: (response: Response) => void;
  apiFetch.mockResolvedValueOnce(answer(status(true)))
    .mockImplementationOnce(() => new Promise<Response>(resolve => { finishFirst = resolve; }))
    .mockResolvedValueOnce(answer({ ok: true, mtime: 2 }))
    .mockImplementationOnce(() => new Promise<Response>(resolve => { finishLast = resolve; }));
  const view = await page('en', true);
  fireEvent.click(await screen.findByRole('button', { name: 'before_search.py', exact: true }));
  fireEvent.click(screen.getByRole('button', { name: 'after_read.py', exact: true }));
  await waitFor(() => expect(launchApp).toHaveBeenCalledOnce());
  expect(launchApp.mock.calls[0][0].params.filename).toBe('after_read.py');
  await act(async () => finishFirst(answer({ ok: true, mtime: 1 })));
  expect(launchApp).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'after_bash.py', exact: true }));
  view.unmount();
  await act(async () => finishLast(answer({ ok: true, mtime: 3 })));
  expect(launchApp).toHaveBeenCalledOnce();
});
