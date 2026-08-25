/* @vitest-environment jsdom */

import { useEffect } from 'react';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { ApiProvider, useApi } from './ApiContext';

let capturedApi: ReturnType<typeof useApi> | null = null;

const CaptureApi = () => {
  const api = useApi();
  useEffect(() => {
    capturedApi = api;
  }, [api]);
  return null;
};

beforeEach(() => {
  capturedApi = null;
  window.localStorage.clear();
  apiFetch.mockReset();
  apiFetch.mockResolvedValue(new Response(JSON.stringify({
    status: 'ok',
    items: [],
    count: 0,
    total_count: 0,
    warnings: [],
  }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }));
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('ApiProvider Memory listing transport', () => {
  it('[MEMORY-LIST-003][MEMORY-LIST-004] keeps page and aggregate cursor payloads distinct', async () => {
    render(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());

    await capturedApi!.listMemoryEpisodes('notes', {
      page: 3,
      cursor: 'ignored-for-single-project',
      limit: 7,
      origin: 'agent',
    });
    await capturedApi!.listMemoryEpisodes('all', {
      page: 9,
      cursor: 'opaque-page-2',
      limit: 20,
    });
    await capturedApi!.listMemoryEpisodes('all');

    expect(apiFetch.mock.calls.map(([path, init]) => ({
      path,
      body: JSON.parse(String(init?.body)),
    }))).toEqual([
      { path: '/api/memory/list', body: { project: 'notes', limit: 7, page: 3, origin: 'agent' } },
      { path: '/api/memory/list', body: { project: 'all', limit: 20, cursor: 'opaque-page-2' } },
      { path: '/api/memory/list', body: { project: 'all', limit: 20 } },
    ]);
  });
});
