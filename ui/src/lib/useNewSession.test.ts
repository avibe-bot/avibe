import { describe, expect, it, vi } from 'vitest';

import type { VibeAgentBrief, WorkbenchProject } from '../context/ApiContext';
import {
  createLatestAgentProjectionLoader,
  isProjectDefaultAgentAvailable,
  projectDefaultAgentRoute,
} from './useNewSession';

const project = (agentName: string): WorkbenchProject =>
  ({
    default_agent: { agent_name: agentName },
  }) as WorkbenchProject;

const agent = (over: Partial<VibeAgentBrief>): VibeAgentBrief =>
  ({
    id: 'agent-1',
    name: 'pm',
    display_name: 'pm',
    description: null,
    backend: 'claude',
    model: null,
    reasoning_effort: null,
    enabled: true,
    archived: false,
    archived_at: null,
    source: 'user',
    updated_at: '2026-08-01T00:00:00Z',
    ...over,
  }) as VibeAgentBrief;

describe('isProjectDefaultAgentAvailable', () => {
  it('accepts an enabled live project default', () => {
    expect(isProjectDefaultAgentAvailable(project('pm'), [agent({})])).toBe(true);
  });

  it('accepts a legacy normalized-equivalent project default', () => {
    expect(
      isProjectDefaultAgentAvailable(
        project('PROJECT-MANAGER'),
        [agent({ name: 'Project Manager' })],
      ),
    ).toBe(true);
  });

  it('rejects an archived internal project default that is absent from the live catalog', () => {
    expect(isProjectDefaultAgentAvailable(project('_pm-8dd7'), [agent({ name: 'codex' })])).toBe(false);
  });

  it('rejects disabled or archived catalog entries', () => {
    expect(isProjectDefaultAgentAvailable(project('pm'), [agent({ enabled: false })])).toBe(false);
    expect(isProjectDefaultAgentAvailable(project('pm'), [agent({ archived: true })])).toBe(false);
  });
});

describe('projectDefaultAgentRoute', () => {
  it('pins the project Agent by stable ID for session creation', () => {
    const target = project('pm');
    target.default_agent = {
      agent_backend: 'claude',
      agent_id: 'agent-original',
      agent_name: 'pm',
      agent_variant: 'claude',
      model: 'claude-opus-5',
      reasoning_effort: 'high',
    };

    expect(projectDefaultAgentRoute(target, [agent({ id: 'agent-replacement' })])).toEqual({
      agent_name: 'pm',
      agent_id: 'agent-original',
      agent_variant: 'claude',
      model: 'claude-opus-5',
      reasoning_effort: 'high',
    });
  });
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

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
    const setProjectionLoaded = vi.fn();
    const loader = createLatestAgentProjectionLoader(fetchAgents, applyProjection, setProjectionLoaded);

    const initialLoad = loader.load();
    const authorizationRefresh = loader.load();
    expect(setProjectionLoaded.mock.calls).toEqual([[false], [false]]);
    const authorizedProjection = {
      ok: true,
      agents: [agent({ id: 'still-authorized', name: 'still-authorized' })],
      default_agent_name: 'still-authorized',
    };
    refreshed.resolve(authorizedProjection);
    await authorizationRefresh;

    stale.resolve({
      ok: true,
      agents: [agent({ id: 'revoked', name: 'revoked' })],
      default_agent_name: 'revoked',
    });
    await initialLoad;

    expect(applyProjection).toHaveBeenCalledTimes(1);
    expect(applyProjection).toHaveBeenCalledWith(authorizedProjection);
    expect(setProjectionLoaded.mock.calls).toEqual([[false], [false], [true]]);
  });

  it('marks a failed latest projection resolved without applying stale data', async () => {
    const failed = deferred<AgentProjection>();
    const applyProjection = vi.fn();
    const setProjectionLoaded = vi.fn();
    const loader = createLatestAgentProjectionLoader(
      () => failed.promise,
      applyProjection,
      setProjectionLoaded,
    );

    const load = loader.load();
    failed.reject(new Error('network failed'));
    await load;

    expect(applyProjection).toHaveBeenCalledWith(null);
    expect(setProjectionLoaded.mock.calls).toEqual([[false], [true]]);
  });
});
