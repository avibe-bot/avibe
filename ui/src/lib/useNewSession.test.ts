import { describe, expect, it, vi } from 'vitest';

import type { VibeAgentBrief } from '../context/ApiContext';
import { createLatestAgentProjectionLoader } from './useNewSession';

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

const agent = (name: string): VibeAgentBrief => ({
  id: name,
  name,
  description: null,
  backend: 'codex',
  model: null,
  reasoning_effort: null,
  enabled: true,
  source: 'user',
  updated_at: '2026-01-01T00:00:00Z',
});

type AgentProjection = {
  ok: boolean;
  agents: VibeAgentBrief[];
  default_agent_name: string | null;
};

describe('createLatestAgentProjectionLoader', () => {
  it('ignores a pre-revocation response that completes after the refreshed projection', async () => {
    const stale = deferred<AgentProjection>();
    const refreshed = deferred<AgentProjection>();
    const fetchAgents = vi
      .fn()
      .mockImplementationOnce(() => stale.promise)
      .mockImplementationOnce(() => refreshed.promise);
    const applyProjection = vi.fn();
    const loader = createLatestAgentProjectionLoader(fetchAgents, applyProjection);

    const initialLoad = loader.load();
    const authorizationRefresh = loader.load();
    const authorizedProjection = {
      ok: true,
      agents: [agent('still-authorized')],
      default_agent_name: 'still-authorized',
    };
    refreshed.resolve(authorizedProjection);
    await authorizationRefresh;

    stale.resolve({
      ok: true,
      agents: [agent('revoked')],
      default_agent_name: 'revoked',
    });
    await initialLoad;

    expect(applyProjection).toHaveBeenCalledTimes(1);
    expect(applyProjection).toHaveBeenCalledWith(authorizedProjection);
  });
});
