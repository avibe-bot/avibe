/* @vitest-environment jsdom */

import { useEffect } from 'react';
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const apiFetch = vi.hoisted(() => vi.fn());
const showToast = vi.hoisted(() => vi.fn());

vi.mock('../lib/apiFetch', () => ({ apiFetch }));
vi.mock('./ToastContext', () => ({ useToast: () => ({ showToast }) }));
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

const response = (payload: unknown, status = 200) => new Response(JSON.stringify(payload), {
  status,
  headers: { 'Content-Type': 'application/json' },
});

const fullAgent = (systemPrompt: string) => ({
  ok: true,
  default_agent_name: 'agent-a',
  agent: {
    id: 'agent-a-id',
    name: 'agent-a',
    display_name: 'agent-a',
    description: 'Agent A',
    backend: 'codex',
    model: 'gpt-5',
    reasoning_effort: 'medium',
    system_prompt: systemPrompt,
    enabled: true,
    archived: false,
    archived_at: null,
    source: 'custom',
    created_at: '2026-08-21T00:00:00Z',
    updated_at: '2026-08-21T00:00:00Z',
    metadata: {},
  },
});

beforeEach(() => {
  capturedApi = null;
  window.localStorage.clear();
  apiFetch.mockReset();
  showToast.mockReset();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe('ApiProvider agent detail transport', () => {
  it('bypasses the cached in-flight detail read when explicitly requested', async () => {
    render(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());

    let resolveCached!: (value: Response) => void;
    const cachedRead = new Promise<Response>((resolve) => {
      resolveCached = resolve;
    });
    apiFetch.mockReturnValueOnce(cachedRead).mockResolvedValueOnce(response(fullAgent('fresh')));

    const cached = capturedApi!.getVibeAgent('agent-a');
    const fresh = capturedApi!.getVibeAgent('agent-a', { cache: false });

    await expect(fresh).resolves.toMatchObject({ agent: { system_prompt: 'fresh' } });
    resolveCached(response(fullAgent('stale')));
    await expect(cached).resolves.toMatchObject({ agent: { system_prompt: 'stale' } });

    expect(apiFetch).toHaveBeenCalledTimes(2);
    expect(apiFetch.mock.calls.map(([path]) => path)).toEqual([
      '/api/agents/agent-a',
      '/api/agents/agent-a',
    ]);
  });

  it('passes expected disappearance codes through the uncached error boundary', async () => {
    render(<ApiProvider><CaptureApi /></ApiProvider>);
    await waitFor(() => expect(capturedApi).not.toBeNull());

    apiFetch.mockResolvedValueOnce(response({
      ok: false,
      code: 'agent_not_found',
      message: 'agent disappeared',
    }, 404));
    await expect(capturedApi!.getVibeAgent('agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found'],
    })).rejects.toMatchObject({ code: 'agent_not_found' });
    expect(showToast).not.toHaveBeenCalled();

    apiFetch.mockResolvedValueOnce(response({
      ok: false,
      code: 'agent_access_forbidden',
      message: 'agent forbidden',
    }, 403));
    await expect(capturedApi!.getVibeAgent('agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found'],
    })).rejects.toMatchObject({ code: 'agent_access_forbidden' });
    expect(showToast).toHaveBeenCalledTimes(1);
  });
});
