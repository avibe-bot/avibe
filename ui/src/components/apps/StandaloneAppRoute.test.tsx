/** @vitest-environment jsdom */
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

type Handlers = {
  onConnected?: () => void;
  onSessionActivity?: (data: { event: string; session_id: string }) => void;
};

const handlers = new Set<Handlers>();
const getSessionResult = vi.fn(async (id: string) => ({ status: 200, session: { id, status: 'active' } }));
const api = {
  getSessionResult,
  connectWorkbenchEvents: (next: Handlers) => {
    handlers.add(next);
    return () => handlers.delete(next);
  },
};

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../context/StandaloneAppTabContext', () => ({ useStandaloneAppTab: () => true }));
vi.mock('../../apps/useAppLaunchSnapshot', () => ({ useAppLaunchSnapshot: () => null }));
vi.mock('../workbench/AppsEditorPage', () => ({ AppsEditorPage: () => null }));
vi.mock('../../apps/registry', () => ({ APP_REGISTRY: { showpage: { Component: () => null } } }));
vi.mock('../workbench/ShowPageAnnotationHost', () => ({
  ShowPageAnnotationHost: ({ src, children }: { src: string | null; children: ReactNode }) => (
    <div data-testid="show-host" data-src={src ?? ''}>{children}</div>
  ),
}));

import { StandaloneAppRoute } from './StandaloneAppRoute';

function renderShow(sessionId: string) {
  render(
    <MemoryRouter initialEntries={[`/apps/show/${sessionId}`]}>
      <Routes>
        <Route path="/apps/show/:sessionId" element={<StandaloneAppRoute appId="showpage" />} />
      </Routes>
    </MemoryRouter>,
  );
  return () => screen.getByTestId('show-host').getAttribute('data-src');
}

const emit = (event: Parameters<NonNullable<Handlers['onSessionActivity']>>[0]) =>
  act(() => handlers.forEach((handler) => handler.onSessionActivity?.(event)));

afterEach(() => {
  cleanup();
  handlers.clear();
  getSessionResult.mockClear();
});

describe('standalone Show window lifecycle', () => {
  it('withdraws the page when its session is archived, ignoring other sessions', async () => {
    const src = renderShow('ses-live');
    await waitFor(() => expect(src()).not.toBe(''));
    emit({ event: 'archived', session_id: 'ses-other' });
    expect(src()).not.toBe('');
    emit({ event: 'archived', session_id: 'ses-live' });
    expect(src()).toBe('');
  });

  it('re-reads the session on every event-stream (re)connect', async () => {
    const src = renderShow('ses-reconnect');
    await waitFor(() => expect(src()).not.toBe(''));
    expect(getSessionResult).toHaveBeenCalledTimes(1);
    getSessionResult.mockResolvedValueOnce({ status: 200, session: { id: 'ses-reconnect', status: 'archived' } });
    act(() => handlers.forEach((handler) => handler.onConnected?.()));
    await waitFor(() => expect(src()).toBe(''));
    expect(getSessionResult).toHaveBeenCalledTimes(2);
  });
});
