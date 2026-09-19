/* @vitest-environment jsdom */
import { createInstance } from 'i18next';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import en from '../../../i18n/en.json';
import { RouteSurfaceActivityBoundary } from '../../RouteSurfaceActivityBoundary';
import { SearchPalette } from './SearchPalette';

const api = vi.hoisted(() => ({ searchMessages: vi.fn(), connectWorkbenchEvents: vi.fn() }));
const inventory = vi.hoisted(() => ({ demand: vi.fn() }));
vi.mock('../../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../useShowPages', async () => {
  const { useEffect } = await import('react');
  return { useShowPageInventory: () => {
    useEffect(() => { inventory.demand(true); return () => inventory.demand(false); }, []);
    return { pages: [], loading: false, loaded: true };
  } };
});
vi.mock('../../../context/WindowManagerContext', () => ({ useWindowManager: () => ({ openApp: vi.fn() }) }));
const i18n = createInstance();
void i18n.use(initReactI18next).init({ lng: 'en', resources: { en: { translation: en } }, interpolation: { escapeValue: false } });
const results = { sessions: [{ session_id: 's1', title: 'Search fixture', project_name: '项目', archived: true,
  matches: [1, 2].map((id) => ({ id: `m${id}`, snippet: { prefix: '', match: `星河结果${id}`, suffix: '' }, author: 'user', type: 'user', source: 'user', created_at: '2026-09-19T00:00:00Z' })) }] };
const queryInput = () => screen.getByRole('textbox') as HTMLInputElement;
const selected = () => document.querySelector('[aria-current="true"]')?.textContent;
const view = (active: boolean, open: boolean, close: () => void) => <I18nextProvider i18n={i18n}>
  <MemoryRouter><RouteSurfaceActivityBoundary active={active}>
    <SearchPalette open={open} onClose={close} />
  </RouteSurfaceActivityBoundary></MemoryRouter>
</I18nextProvider>;
beforeEach(() => {
  vi.clearAllMocks();
  api.searchMessages.mockResolvedValue(results);
  api.connectWorkbenchEvents.mockImplementation(() => vi.fn());
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

describe('SearchPalette owner and presentation lifetime', () => {
  it('retains Unicode, archived opt-in and a nonfirst result across repeated suspension; true close starts fresh', async () => {
    const close = vi.fn();
    const rendered = render(view(true, true, close));
    fireEvent.change(queryInput(), { target: { value: '星河 🌱' } });
    fireEvent.click(screen.getByRole('switch'));
    await waitFor(() => expect(selected()).toContain('星河结果1'));
    queryInput().focus();
    fireEvent.keyDown(queryInput(), { key: 'ArrowDown' });
    expect(selected()).toContain('星河结果2');
    for (let round = 0; round < 2; round += 1) {
      rendered.rerender(view(false, true, close));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(inventory.demand).toHaveBeenLastCalledWith(false);
      const requestCount = api.searchMessages.mock.calls.length;
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
      expect(api.searchMessages).toHaveBeenCalledTimes(requestCount);
      fireEvent.keyDown(window, { key: 'Escape' });
      expect(close).not.toHaveBeenCalled();
      rendered.rerender(view(true, true, close));
      expect(queryInput().value).toBe('星河 🌱');
      expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true');
      await waitFor(() => expect(selected()).toContain('星河结果2'));
      expect(document.activeElement).toBe(queryInput());
    }
    rendered.rerender(view(true, false, close));
    rendered.rerender(view(true, true, close));
    expect(queryInput().value).toBe('');
    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false');
  });

  it('retires a pending fetch with the presentation and ignores its late result', async () => {
    let release!: (value: typeof results) => void;
    api.searchMessages.mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    const close = vi.fn();
    const rendered = render(view(true, true, close));
    fireEvent.change(queryInput(), { target: { value: '星河' } });
    await waitFor(() => expect(api.searchMessages).toHaveBeenCalledTimes(1));
    rendered.rerender(view(false, true, close));
    await act(async () => { release(results); });
    expect(screen.queryByRole('dialog')).toBeNull();
    api.searchMessages.mockResolvedValue({ sessions: [] });
    rendered.rerender(view(true, true, close));
    await waitFor(() => expect(api.searchMessages).toHaveBeenCalledTimes(2));
    expect(selected()).toBeUndefined();
    expect(queryInput().value).toBe('星河');
  });

  it('starts inactive with no modal, data subscription or demand', () => {
    render(view(false, true, vi.fn()));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(api.connectWorkbenchEvents).not.toHaveBeenCalled();
    expect(inventory.demand).not.toHaveBeenCalled();
  });
});
