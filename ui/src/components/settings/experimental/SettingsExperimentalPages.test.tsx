/* @vitest-environment jsdom */

// Settings › Experimental (ui/design: settings/source-filter/*): the renamed experiment
// keeps its existing route, API and hook directory. Real catalogs guard the displayed name;
// the former key-only translation stub could not catch a stale name in either language.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '@/i18n/en.json';
import zh from '@/i18n/zh.json';
import { SettingsExperimentalPage, SettingsSourceFilterPage } from './SettingsExperimentalPages';

const apiFetch = vi.hoisted(() => vi.fn());

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
  hooks: { before_search: hook(true), after_search: hook(false), before_fetch: hook(false), after_fetch: hook(false) },
});
const answer = (body: unknown) => ({ ok: true, status: 200, json: async () => body });

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
    expect(screen.getAllByText(missing)).toHaveLength(3);
  });
});
