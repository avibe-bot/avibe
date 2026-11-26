/* @vitest-environment jsdom */

// Settings › Experimental (ui/design: settings/source-filter/*): the list row only opens the
// experiment's page, and the page's switch is the one control that writes the setting.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SettingsExperimentalPage, SettingsSourceFilterPage, SOURCE_FILTER_PATH } from './SettingsExperimentalPages';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('@/lib/apiFetch', () => ({ apiFetch }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key, i18n: { language: 'zh' } }),
}));

const hook = (found: boolean) => ({ found, last_failure: null });
const status = (enabled: boolean) => ({
  ok: true,
  enabled,
  dir: '/home/demo/.avibe/hooks/source_filter',
  hooks: { before_search: hook(true), after_search: hook(false), before_fetch: hook(false), after_fetch: hook(false) },
});
const answer = (body: unknown) => ({ ok: true, status: 200, json: async () => body });

afterEach(() => {
  cleanup();
  apiFetch.mockReset();
});

describe('Settings › Experimental', () => {
  it('lists Source filter as a row that opens its page and carries no switch', async () => {
    apiFetch.mockResolvedValue(answer(status(true)));
    render(<MemoryRouter><SettingsExperimentalPage /></MemoryRouter>);
    const row = await screen.findByRole('link', { name: /settings\.experimental\.sourceFilter\.title/ });
    expect(row.getAttribute('href')).toBe(SOURCE_FILTER_PATH);
    await screen.findByText('settings.experimental.on');
    expect(screen.queryByRole('switch')).toBeNull();
  });

  it('turns the source filter on from its page and then shows the scripts', async () => {
    apiFetch.mockResolvedValueOnce(answer(status(false))).mockResolvedValueOnce(answer(status(true)));
    render(<MemoryRouter><SettingsSourceFilterPage /></MemoryRouter>);
    const toggle = await screen.findByRole('switch');
    await waitFor(() => expect((toggle as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByText('before_search.py')).toBeNull();
    fireEvent.click(toggle);
    await screen.findByText('before_search.py');
    const [path, init] = apiFetch.mock.calls[1];
    expect(path).toBe('/api/settings/experimental/source-filter');
    expect(init).toMatchObject({ method: 'PUT', body: JSON.stringify({ enabled: true }) });
    expect(screen.getAllByText('settings.experimental.sourceFilter.notFound')).toHaveLength(3);
  });
});
