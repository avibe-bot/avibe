/* @vitest-environment jsdom */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import type { VibeAgentBrief, WorkbenchEventHandlers } from '../../context/ApiContext';
import type { InstanceCapabilities, InstanceRole } from '../../lib/sessionInfo';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { AgentsPage } from './AgentsPage';

type FakeApi = {
  listVibeAgents: ReturnType<typeof vi.fn>;
  getVibeAgent: ReturnType<typeof vi.fn>;
  getVibeAgentOnboarding: ReturnType<typeof vi.fn>;
  onboardVibeAgents: ReturnType<typeof vi.fn>;
  updateVibeAgent: ReturnType<typeof vi.fn>;
  removeVibeAgent: ReturnType<typeof vi.fn>;
  getRunningAgents: ReturnType<typeof vi.fn>;
  connectWorkbenchEvents: ReturnType<typeof vi.fn>;
};

const apiRef = vi.hoisted(() => ({ current: null as FakeApi | null }));
const showToast = vi.hoisted(() => vi.fn());
let handlers: WorkbenchEventHandlers | null = null;

vi.stubGlobal('ResizeObserver', class {
  observe() {}
  unobserve() {}
  disconnect() {}
});

vi.mock('../../context/ApiContext', async () => {
  const actual = await vi.importActual<typeof import('../../context/ApiContext')>('../../context/ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('./CapabilityTabs', () => ({ CapabilityTabs: () => null }));
// What the catalog answers for this render. The default is an empty list, which
// is every test that types its own model; a test about per-model efforts states
// the entries the Hub catalog would have projected.
type FakeModelCatalog = {
  models: string[];
  reasoningOptions?: Record<string, { value: string; label: string }[]>;
};
let modelCatalog: FakeModelCatalog = { models: [] };

vi.mock('../../lib/backendModels', async () => ({
  loadBackendModelsWithRefresh: (_api: unknown, _backend: string, onLoaded: (payload: FakeModelCatalog) => void) => {
    onLoaded(modelCatalog);
    return () => {};
  },
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
  }),
}));

const brief = (name: string, description: string): VibeAgentBrief => ({
  id: `id-${name}`,
  name,
  display_name: name,
  description,
  backend: 'codex',
  model: null,
  reasoning_effort: null,
  enabled: true,
  archived: false,
  archived_at: null,
  source: 'custom',
  updated_at: '2026-08-21T00:00:00Z',
});

const listResult = (agents: VibeAgentBrief | VibeAgentBrief[]) => ({
  ok: true,
  agents: Array.isArray(agents) ? agents : [agents],
  default_agent_name: Array.isArray(agents) ? agents[0]?.name ?? null : agents.name,
});

const fullAgent = (briefAgent: VibeAgentBrief, systemPrompt: string) => ({
  ok: true,
  default_agent_name: briefAgent.name,
  agent: {
    ...briefAgent,
    model: briefAgent.model ?? 'gpt-5',
    reasoning_effort: briefAgent.reasoning_effort ?? 'medium',
    system_prompt: systemPrompt,
    created_at: '2026-08-21T00:00:00Z',
    metadata: {},
  },
});

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((next, fail) => {
    resolve = next;
    reject = fail;
  });
  return { promise, resolve, reject };
};

function makeApi(
  listVibeAgents: FakeApi['listVibeAgents'],
  getVibeAgent: FakeApi['getVibeAgent'] = vi.fn().mockResolvedValue({ ok: false }),
  getVibeAgentOnboarding: FakeApi['getVibeAgentOnboarding'] = vi.fn().mockResolvedValue({ available: false }),
  onboardVibeAgents: FakeApi['onboardVibeAgents'] = vi.fn().mockResolvedValue({ available: false }),
  updateVibeAgent: FakeApi['updateVibeAgent'] = vi.fn().mockResolvedValue({ ok: false }),
  removeVibeAgent: FakeApi['removeVibeAgent'] = vi.fn().mockResolvedValue({ ok: true }),
): FakeApi {
  return {
    listVibeAgents,
    getVibeAgent,
    getVibeAgentOnboarding,
    onboardVibeAgents,
    updateVibeAgent,
    removeVibeAgent,
    getRunningAgents: vi.fn().mockResolvedValue({ ok: true, counts: { total: 2 } }),
    connectWorkbenchEvents: vi.fn((next: WorkbenchEventHandlers) => {
      handlers = next;
      return vi.fn();
    }),
  };
}

const MEMBER_CAPABILITIES: InstanceCapabilities = {
  ...OWNER_INSTANCE_CAPABILITIES,
  is_instance_owner: false,
  can_manage_instance: false,
  can_manage_access_members: false,
};

function renderPage(
  api: FakeApi,
  {
    remote = false,
    instanceKind = null,
    instanceRole = 'owner' as InstanceRole,
    canManageAgents = false,
    capabilities,
  }: {
    remote?: boolean;
    instanceKind?: 'organization' | 'personal' | null;
    instanceRole?: InstanceRole;
    canManageAgents?: boolean;
    capabilities?: InstanceCapabilities;
  } = {},
) {
  apiRef.current = api;
  const effectiveCapabilities = capabilities ?? { ...OWNER_INSTANCE_CAPABILITIES, can_manage_agents: canManageAgents };
  return render(
    <InstanceAuthorizationContext.Provider
      value={{ remote, instanceKind, instanceRole, capabilities: effectiveCapabilities }}
    >
      <MemoryRouter initialEntries={['/agents']}>
        <AgentsPage />
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  apiRef.current = null;
  handlers = null;
  showToast.mockReset();
  modelCatalog = { models: [] };
});

describe('AgentsPage load requests follow the rank that can serve them', () => {
  it('does not request the Owner-only onboarding inventory for a member', async () => {
    const listVibeAgents = vi.fn().mockResolvedValue(listResult(brief('claude', '')));
    const api = makeApi(listVibeAgents);
    const view = renderPage(api, {
      remote: true,
      instanceKind: 'organization',
      instanceRole: 'member',
      capabilities: MEMBER_CAPABILITIES,
    });

    await waitFor(() => expect(api.listVibeAgents).toHaveBeenCalled());
    await waitFor(() => expect(api.getVibeAgent).toHaveBeenCalled());
    expect(api.getVibeAgentOnboarding).not.toHaveBeenCalled();
    view.unmount();
  });

  it('still requests the onboarding inventory for the instance owner', async () => {
    const listVibeAgents = vi.fn().mockResolvedValue(listResult(brief('claude', '')));
    const api = makeApi(listVibeAgents);
    const view = renderPage(api, {
      remote: true,
      instanceKind: 'organization',
      instanceRole: 'owner',
      capabilities: OWNER_INSTANCE_CAPABILITIES,
    });

    await waitFor(() => expect(api.getVibeAgentOnboarding).toHaveBeenCalled());
    view.unmount();
  });
});

describe('AgentsPage reconnect reconciliation', () => {
  it('refreshes definitions from the server on the gap edge without bridge-status duplication', async () => {
    const stale = brief('stale-agent', 'before the gap');
    const fresh = brief('fresh-agent', 'changed during the gap');
    const listVibeAgents = vi.fn()
      .mockResolvedValueOnce(listResult(stale))
      .mockResolvedValueOnce(listResult(fresh));
    const api = makeApi(listVibeAgents);
    renderPage(api);

    await waitFor(() => expect(screen.getByText('stale-agent')).toBeTruthy());
    expect(handlers).not.toBeNull();

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByText('fresh-agent')).toBeTruthy());

    expect(listVibeAgents).toHaveBeenNthCalledWith(1, { includeDisabled: true, cache: false });
    expect(listVibeAgents).toHaveBeenNthCalledWith(2, { includeDisabled: true, cache: false });
    expect(api.getRunningAgents).toHaveBeenCalledTimes(2);

    act(() => {
      handlers?.onEventBridgeStatus?.({ connected: false });
      handlers?.onEventBridgeStatus?.({ connected: true });
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(listVibeAgents).toHaveBeenCalledTimes(2);
  });

  it('does not retry a failed reconciliation debt until the next reconnect edge', async () => {
    const agent = brief('agent-a', 'A');
    const update = vi.fn().mockResolvedValue(fullAgent(agent, 'ack'));
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial'))
      .mockRejectedValueOnce({ code: 'agent_backend_unavailable', message: 'temporary failure' })
      .mockResolvedValueOnce(fullAgent({ ...agent, description: 'after reconnect' }, 'after reconnect'));
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, update);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A'), { target: { value: 'A edited' } });
    fireEvent.blur(screen.getByDisplayValue('A edited'));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByText('temporary failure')).toBeTruthy());
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getVibeAgent).toHaveBeenCalledTimes(2);

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    await waitFor(() => expect(screen.getByDisplayValue('after reconnect')).toBeTruthy());
  });

  it('does not create a follow-on reconciliation request after unmount', async () => {
    const agent = brief('agent-a', 'A');
    const pending = deferred<ReturnType<typeof fullAgent>>();
    const update = vi.fn().mockResolvedValue(fullAgent(agent, 'ack'));
    const getVibeAgent = vi.fn().mockResolvedValueOnce(fullAgent(agent, 'initial')).mockReturnValueOnce(pending.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, update);
    const view = renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A'), { target: { value: 'A edited' } });
    fireEvent.blur(screen.getByDisplayValue('A edited'));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    view.unmount();
    act(() => pending.reject({ code: 'agent_backend_unavailable', message: 'unmounted failure' }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getVibeAgent).toHaveBeenCalledTimes(2);
  });

  it('keeps an explicit detail dismissal closed across reconnect list publication', async () => {
    const agent = brief('agent-a', 'A');
    const listVibeAgents = vi.fn().mockResolvedValue(listResult(agent));
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial'))
      .mockResolvedValueOnce(fullAgent(agent, 'explicit selection'));
    const api = makeApi(listVibeAgents, getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'common.close' }));
    expect(screen.queryByDisplayValue('A')).toBeNull();
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(listVibeAgents).toHaveBeenCalledTimes(2));
    expect(screen.queryByDisplayValue('A')).toBeNull();
    expect(getVibeAgent).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('agent-a').closest('button')!);
    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenCalledTimes(2);
  });

  it('keeps an older pre-gap read from overwriting the post-gap definition snapshot', async () => {
    const stale = brief('stale-agent', 'before the gap');
    const fresh = brief('fresh-agent', 'changed during the gap');
    let resolvePreGap!: (value: ReturnType<typeof listResult>) => void;
    const preGap = new Promise<ReturnType<typeof listResult>>((resolve) => {
      resolvePreGap = resolve;
    });
    const listVibeAgents = vi.fn()
      .mockResolvedValueOnce(listResult(stale))
      .mockReturnValueOnce(preGap)
      .mockResolvedValueOnce(listResult(fresh));
    const api = makeApi(listVibeAgents);
    renderPage(api);

    await waitFor(() => expect(screen.getByText('stale-agent')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'common.refresh' }));
    await waitFor(() => expect(listVibeAgents).toHaveBeenCalledTimes(2));

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByText('fresh-agent')).toBeTruthy());

    resolvePreGap(listResult(stale));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText('fresh-agent')).toBeTruthy();
    expect(screen.queryByText('stale-agent')).toBeNull();
    expect(listVibeAgents).toHaveBeenNthCalledWith(3, { includeDisabled: true, cache: false });
    expect(api.getRunningAgents).toHaveBeenCalledTimes(2);
  });

  it('refreshes the selected full definition and keeps one stable subscription', async () => {
    const first = brief('agent-a', 'before gap');
    const second = brief('agent-b', 'another agent');
    const changed = { ...first, description: 'after gap' };
    const listVibeAgents = vi.fn()
      .mockResolvedValueOnce(listResult([first, second]))
      .mockResolvedValueOnce(listResult([changed, second]));
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(first, 'before prompt'))
      .mockResolvedValueOnce(fullAgent(changed, 'after prompt'))
      .mockResolvedValueOnce(fullAgent(second, 'second prompt'));
    const api = makeApi(listVibeAgents, getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('before gap')).toBeTruthy());
    expect(api.connectWorkbenchEvents).toHaveBeenCalledTimes(1);

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue('after gap')).toBeTruthy());
    fireEvent.click(screen.getByText('agents.detail.systemPrompt').closest('button')!);
    expect(screen.getByDisplayValue('after prompt')).toBeTruthy();
    expect(getVibeAgent).toHaveBeenNthCalledWith(2, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(api.getRunningAgents).toHaveBeenCalledTimes(2);

    const secondRow = screen.getByText('agent-b').closest('button');
    expect(secondRow).not.toBeNull();
    fireEvent.click(secondRow!);
    await waitFor(() => expect(screen.getByDisplayValue('another agent')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-b', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(api.connectWorkbenchEvents).toHaveBeenCalledTimes(1);
  });

  it('reissues the pending B selection at reconnect and suppresses the pre-gap B response', async () => {
    const agentA = brief('agent-a', 'A');
    const agentB = brief('agent-b', 'B');
    const oldB = deferred<ReturnType<typeof fullAgent>>();
    const freshB = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(oldB.promise)
      .mockReturnValueOnce(freshB.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult([agentA, agentB])), getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    expect(getVibeAgent).toHaveBeenNthCalledWith(2, 'agent-b', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-b', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });

    act(() => oldB.resolve(fullAgent({ ...agentB, description: 'old B' }, 'old B response')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('A')).toBeTruthy();
    expect(screen.queryByDisplayValue('old B')).toBeNull();

    act(() => freshB.resolve(fullAgent(agentB, 'B selected')));
    await waitFor(() => expect(screen.getByDisplayValue('B')).toBeTruthy());
    expect(api.connectWorkbenchEvents).toHaveBeenCalledTimes(1);
  });

  it('does not reuse an invalidated A debt stage after B fails', async () => {
    const agentA = brief('agent-a', 'A');
    const agentB = brief('agent-b', 'B');
    const patch = deferred<unknown>();
    const oldDebt = deferred<ReturnType<typeof fullAgent>>();
    const pendingB = deferred<ReturnType<typeof fullAgent>>();
    const freshDebt = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(oldDebt.promise)
      .mockReturnValueOnce(pendingB.promise)
      .mockReturnValueOnce(freshDebt.promise);
    const updateVibeAgent = vi.fn().mockReturnValue(patch.promise);
    const api = makeApi(
      vi.fn().mockResolvedValue(listResult([agentA, agentB])),
      getVibeAgent,
      undefined,
      undefined,
      updateVibeAgent,
    );
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A'), { target: { value: 'A edited' } });
    fireEvent.blur(screen.getByDisplayValue('A edited'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(1));
    act(() => patch.resolve({ ok: true }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    act(() => pendingB.reject({ code: 'agent_backend_unavailable', message: 'B unavailable' }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(4));

    act(() => freshDebt.resolve(fullAgent({ ...agentA, description: 'A after B' }, 'fresh A')));
    await waitFor(() => expect(screen.getByDisplayValue('A after B')).toBeTruthy());
    act(() => oldDebt.resolve(fullAgent(agentA, 'stale A')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('A after B')).toBeTruthy();
    expect(getVibeAgent).toHaveBeenCalledTimes(4);
  });

  it.each(['agent_not_found', 'agent_access_forbidden'] as const)(
    'tombstones a stale row on direct selection without a repeat read (%s)',
    async (code) => {
      const agentA = brief('agent-a', 'A');
      const agentB = brief('agent-b', 'B');
      const getVibeAgent = vi.fn()
        .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
        .mockRejectedValueOnce({ code, message: 'B disappeared' });
      const listVibeAgents = vi.fn().mockResolvedValue(listResult([agentA, agentB]));
      const api = makeApi(listVibeAgents, getVibeAgent);
      renderPage(api);

      await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
      fireEvent.click(screen.getByText('agent-b').closest('button')!);
      await waitFor(() => expect(screen.queryByText('agent-b')).toBeNull());
      expect(screen.getByDisplayValue('A')).toBeTruthy();
      expect(getVibeAgent).toHaveBeenCalledTimes(2);
      expect(listVibeAgents).toHaveBeenCalledTimes(2);
      expect(showToast).not.toHaveBeenCalled();
    },
  );

  it('orders a list-first gap retirement before the same-edge accepted-A read', async () => {
    const agentA = brief('agent-a', 'A before gap');
    const agentB = brief('agent-b', 'B pending');
    const staleB = deferred<ReturnType<typeof fullAgent>>();
    const catchupA = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(staleB.promise)
      .mockReturnValueOnce(catchupA.promise);
    const listVibeAgents = vi.fn()
      .mockResolvedValueOnce(listResult([agentA, agentB]))
      .mockResolvedValueOnce(listResult(agentA));
    const api = makeApi(listVibeAgents, getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A before gap')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(listVibeAgents).toHaveBeenCalledTimes(2);

    act(() => catchupA.resolve(fullAgent({ ...agentA, description: 'A after gap' }, 'A changed prompt')));
    await waitFor(() => expect(screen.getByDisplayValue('A after gap')).toBeTruthy());
    act(() => staleB.resolve(fullAgent(agentB, 'stale B')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('A after gap')).toBeTruthy();
    expect(getVibeAgent.mock.calls.slice(3).some(([name]) => name === 'agent-b')).toBe(false);
  });

  it.each([
    { label: 'rejected read', response: 'reject' as const },
    { label: 'HTTP ok false', response: 'okfalse' as const },
  ])('continues to accepted A after an unexpected B catch-up failure ($label)', async ({ response }) => {
    const agentA = brief('agent-a', 'A before gap');
    const agentB = brief('agent-b', 'B pending');
    const staleB = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(staleB.promise)
      .mockImplementationOnce(() => response === 'reject'
        ? Promise.reject({ code: 'agent_backend_unavailable', message: 'B catch-up failed' })
        : Promise.resolve({ ok: false, message: 'B catch-up failed' }))
      .mockResolvedValueOnce(fullAgent({ ...agentA, description: 'A after failed B' }, 'A recovered'));
    const listVibeAgents = vi.fn().mockResolvedValue(listResult([agentA, agentB]));
    const api = makeApi(listVibeAgents, getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A before gap')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue('A after failed B')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-b', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(getVibeAgent).toHaveBeenNthCalledWith(4, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(listVibeAgents).toHaveBeenCalledTimes(2);
    expect(screen.getByText('B catch-up failed')).toBeTruthy();
    act(() => staleB.resolve(fullAgent(agentB, 'stale B')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('A after failed B')).toBeTruthy();
    expect(getVibeAgent.mock.calls.slice(4).some(([name]) => name === 'agent-b')).toBe(false);
  });

  it('carries a row-tap drill-down intent through a reconnect replacement read', async () => {
    const agentA = brief('agent-a', 'A');
    const agentB = brief('agent-b', 'B');
    const staleB = deferred<ReturnType<typeof fullAgent>>();
    const freshB = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(staleB.promise)
      .mockReturnValueOnce(freshB.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult([agentA, agentB])), getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    act(() => freshB.resolve(fullAgent({ ...agentB, description: 'B after reconnect' }, 'B after reconnect')));
    await waitFor(() => expect(screen.getByDisplayValue('B after reconnect')).toBeTruthy());
    const detail = screen.getByDisplayValue('B after reconnect').closest('.self-start');
    expect(detail?.className).not.toContain('max-lg:hidden');
    act(() => staleB.resolve(fullAgent(agentB, 'stale B')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('B after reconnect')).toBeTruthy();
  });

  it('joins the live debt producer when selecting the current Agent again', async () => {
    const agent = brief('agent-a', 'description');
    const debtRead = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial prompt'))
      .mockReturnValueOnce(debtRead.promise);
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    fireEvent.change(screen.getByPlaceholderText('agents.create.systemPromptPlaceholder'), {
      target: { value: 'saved prompt' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    const row = screen.getAllByText('agent-a').find((node) => node.closest('button'))?.closest('button');
    expect(row).not.toBeNull();
    fireEvent.click(row!);
    expect(getVibeAgent).toHaveBeenCalledTimes(2);

    act(() => debtRead.resolve(fullAgent({ ...agent, description: 'after debt' }, 'saved prompt')));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(api.listVibeAgents).toHaveBeenCalledTimes(2));
    expect(screen.getByDisplayValue('after debt')).toBeTruthy();
    expect(updateVibeAgent).toHaveBeenCalledTimes(1);
  });

  it('joins a pending reconnect producer when selecting the current Agent again', async () => {
    const agent = brief('agent-a', 'before');
    const reconnectRead = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial prompt'))
      .mockReturnValueOnce(reconnectRead.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('before')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    const row = screen.getAllByText('agent-a').find((node) => node.closest('button'))?.closest('button');
    expect(row).not.toBeNull();
    fireEvent.click(row!);
    expect(getVibeAgent).toHaveBeenCalledTimes(2);

    act(() => reconnectRead.resolve(fullAgent({ ...agent, description: 'after reconnect' }, 'fresh prompt')));
    await waitFor(() => expect(screen.getByDisplayValue('after reconnect')).toBeTruthy());
    const detail = screen.getByDisplayValue('after reconnect').closest('.self-start');
    expect(detail?.className).not.toContain('max-lg:hidden');
  });

  it('does not let a lower-floor selection read satisfy later mutation debt', async () => {
    const agent = brief('agent-a', 'before');
    const selectionRead = deferred<ReturnType<typeof fullAgent>>();
    const debtRead = deferred<ReturnType<typeof fullAgent>>();
    const patch = deferred<unknown>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial prompt'))
      .mockReturnValueOnce(selectionRead.promise)
      .mockReturnValueOnce(debtRead.promise);
    const updateVibeAgent = vi.fn().mockReturnValue(patch.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('before')).toBeTruthy());
    const row = screen.getAllByText('agent-a').find((node) => node.closest('button'))?.closest('button');
    expect(row).not.toBeNull();
    fireEvent.click(row!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    const description = screen.getByDisplayValue('before');
    fireEvent.change(description, { target: { value: 'local mutation' } });
    fireEvent.blur(description);
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { description: 'local mutation' }));
    act(() => patch.resolve({ ok: true }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));

    act(() => selectionRead.resolve(fullAgent({ ...agent, description: 'stale selection' }, 'stale prompt')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('local mutation')).toBeTruthy();

    act(() => debtRead.resolve(fullAgent({ ...agent, description: 'authoritative mutation' }, 'fresh prompt')));
    await waitFor(() => expect(screen.getByDisplayValue('authoritative mutation')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenCalledTimes(3);
  });

  it.each([
    { label: 'rejected debt read', rejected: true },
    { label: 'ok-false debt read', rejected: false },
  ])('settles every same-agent consumer when the joined stage has a $label', async ({ rejected }) => {
    const agent = brief('agent-a', 'description');
    const debtRead = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial prompt'))
      .mockReturnValueOnce(debtRead.promise);
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    fireEvent.change(screen.getByPlaceholderText('agents.create.systemPromptPlaceholder'), {
      target: { value: 'joined prompt' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    const row = screen.getAllByText('agent-a').find((node) => node.closest('button'))?.closest('button');
    expect(row).not.toBeNull();
    fireEvent.click(row!);
    expect(getVibeAgent).toHaveBeenCalledTimes(2);

    if (rejected) {
      act(() => debtRead.reject({ message: 'joined debt failed' }));
    } else {
      act(() => debtRead.resolve({ ok: false, message: 'joined debt failed' } as never));
    }
    await waitFor(() => expect(screen.getByRole('dialog')).toBeTruthy());
    expect(screen.getByText('joined debt failed')).toBeTruthy();
    expect(updateVibeAgent).toHaveBeenCalledTimes(1);
  });

  it('lets a successful PATCH win over a reconnect GET that settles later', async () => {
    const initial = { ...brief('agent-a', 'before patch'), model: 'old-model' };
    const server = { ...initial, description: 'server description', model: 'server-model' };
    const patchResult = fullAgent({ ...initial, description: 'local patch', model: 'patched-model' }, 'patched prompt');
    const staleRead = deferred<ReturnType<typeof fullAgent>>();
    const drainRead = deferred<ReturnType<typeof fullAgent>>();
    const patch = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'before prompt'))
      .mockReturnValueOnce(staleRead.promise)
      .mockReturnValueOnce(drainRead.promise);
    const listVibeAgents = vi.fn().mockResolvedValue(listResult(server));
    const updateVibeAgent = vi.fn().mockReturnValue(patch.promise);
    const api = makeApi(listVibeAgents, getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('before patch')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    const description = screen.getByDisplayValue('before patch');
    fireEvent.change(description, { target: { value: 'local patch' } });
    fireEvent.blur(description);
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { description: 'local patch' }));
    act(() => patch.resolve(patchResult));
    await waitFor(() => expect(screen.getByDisplayValue('local patch')).toBeTruthy());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(screen.getByRole('combobox', { hidden: true }).textContent).toContain('old-model');

    act(() => staleRead.resolve(fullAgent(initial, 'stale reconnect prompt')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('local patch')).toBeTruthy();
    expect(screen.queryByDisplayValue('server description')).toBeNull();

    act(() => drainRead.resolve(fullAgent(server, 'authoritative prompt')));
    await waitFor(() => expect(screen.getByRole('combobox', { hidden: true }).textContent).toContain('server-model'));
    expect(screen.getByDisplayValue('server description')).toBeTruthy();
  });

  it('uses operation epochs to suppress an old A -> B -> A selection response', async () => {
    const agentA = brief('agent-a', 'A before');
    const agentB = brief('agent-b', 'B');
    const oldA = deferred<ReturnType<typeof fullAgent>>();
    const readB = deferred<ReturnType<typeof fullAgent>>();
    const readAAgain = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(oldA.promise)
      .mockReturnValueOnce(readB.promise)
      .mockReturnValueOnce(readAAgain.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult([agentA, agentB])), getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A before')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    const agentARow = screen.getAllByText('agent-a').find((node) => node.closest('button'))?.closest('button');
    expect(agentARow).not.toBeNull();
    fireEvent.click(agentARow!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(4));

    act(() => oldA.resolve(fullAgent(agentA, 'old A response')));
    act(() => readB.resolve(fullAgent(agentB, 'B response')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('A before')).toBeTruthy();

    act(() => readAAgain.resolve(fullAgent({ ...agentA, description: 'A after ABA' }, 'new A response')));
    await waitFor(() => expect(screen.getByDisplayValue('A after ABA')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenNthCalledWith(2, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-b', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(getVibeAgent).toHaveBeenNthCalledWith(4, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
  });

  it.each([
    { label: 'A succeeds', failA: false },
    { label: 'A fails', failA: true },
  ])('isolates field settlement across A/B panel instances when $label', async ({ failA }) => {
    const agentA = brief('agent-a', 'A initial');
    const agentB = brief('agent-b', 'B initial');
    const readB = deferred<ReturnType<typeof fullAgent>>();
    const drainB = deferred<ReturnType<typeof fullAgent>>();
    const patchA = deferred<unknown>();
    const patchB = deferred<unknown>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A prompt'))
      .mockReturnValueOnce(readB.promise)
      .mockReturnValueOnce(drainB.promise);
    const updateVibeAgent = vi.fn()
      .mockReturnValueOnce(patchA.promise)
      .mockReturnValueOnce(patchB.promise);
    const api = makeApi(
      vi.fn().mockResolvedValue(listResult([agentA, agentB])),
      getVibeAgent,
      undefined,
      undefined,
      updateVibeAgent,
    );
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A initial')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A initial'), { target: { value: 'A local' } });
    fireEvent.blur(screen.getByDisplayValue('A local'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { description: 'A local' }));

    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => readB.resolve(fullAgent(agentB, 'B prompt')));
    await waitFor(() => expect(screen.getByDisplayValue('B initial')).toBeTruthy());

    fireEvent.change(screen.getByDisplayValue('B initial'), { target: { value: 'B local' } });
    fireEvent.blur(screen.getByDisplayValue('B local'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-b', { description: 'B local' }));

    act(() => (failA ? patchA.reject({ message: 'A failed' }) : patchA.resolve({ ok: true })));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('B local')).toBeTruthy();

    act(() => patchB.resolve({ ok: true }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    act(() => drainB.resolve(fullAgent({ ...agentB, description: 'B authoritative' }, 'B final prompt')));
    await waitFor(() => expect(screen.getByDisplayValue('B authoritative')).toBeTruthy());
  });

  it('keeps the current selection intent when DELETE fails', async () => {
    const agent = brief('agent-a', 'A');
    const reconnect = deferred<ReturnType<typeof fullAgent>>();
    const removeVibeAgent = vi.fn().mockResolvedValue({ ok: false, message: 'delete rejected' });
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial'))
      .mockReturnValueOnce(reconnect.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, undefined, removeVibeAgent);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'common.delete' }));
    await waitFor(() => expect(removeVibeAgent).toHaveBeenCalledWith('agent-a'));
    expect(screen.getByDisplayValue('A')).toBeTruthy();

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => reconnect.resolve(fullAgent({ ...agent, description: 'A refreshed' }, 'reconciled')));
    await waitFor(() => expect(screen.getByDisplayValue('A refreshed')).toBeTruthy());
  });

  it('does not let successful DELETE cancel a newer pending selection', async () => {
    const agentA = brief('agent-a', 'A');
    const agentB = brief('agent-b', 'B');
    const pendingB = deferred<ReturnType<typeof fullAgent>>();
    const removeVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(pendingB.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult([agentA, agentB])), getVibeAgent, undefined, undefined, undefined, removeVibeAgent);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: 'common.delete' }));
    await waitFor(() => expect(removeVibeAgent).toHaveBeenCalledWith('agent-a'));
    act(() => pendingB.resolve(fullAgent(agentB, 'B selected')));
    await waitFor(() => expect(screen.getByDisplayValue('B')).toBeTruthy());
    expect(screen.queryByDisplayValue('A initial')).toBeNull();
  });

  it('rolls a failed current selection back to the accepted Agent', async () => {
    const agentA = brief('agent-a', 'A');
    const agentB = brief('agent-b', 'B');
    const failedB = deferred<ReturnType<typeof fullAgent>>();
    const reconnectA = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(failedB.promise)
      .mockReturnValueOnce(reconnectA.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult([agentA, agentB])), getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => failedB.reject({ code: 'agent_backend_unavailable', message: 'B unavailable' }));
    await waitFor(() => expect(screen.getByText('B unavailable')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    act(() => reconnectA.resolve(fullAgent({ ...agentA, description: 'A after rollback' }, 'A reconciled')));
    await waitFor(() => expect(screen.getByDisplayValue('A after rollback')).toBeTruthy());
  });

  it('treats concurrent PATCH responses as acknowledgements and drains once', async () => {
    const initial = { ...brief('agent-a', 'base description'), model: 'base-model', enabled: true };
    const patchOne = deferred<ReturnType<typeof fullAgent>>();
    const patchTwo = deferred<ReturnType<typeof fullAgent>>();
    const drain = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'base prompt'))
      .mockReturnValueOnce(drain.promise);
    const updateVibeAgent = vi.fn()
      .mockReturnValueOnce(patchOne.promise)
      .mockReturnValueOnce(patchTwo.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('base description')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('base description'), { target: { value: 'local description' } });
    fireEvent.blur(screen.getByDisplayValue('local description'));
    fireEvent.click(screen.getByRole('switch'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(2));

    act(() => patchTwo.resolve(fullAgent({ ...initial, description: 'wrong second', enabled: false }, 'wrong second prompt')));
    act(() => patchOne.resolve(fullAgent({ ...initial, description: 'wrong first', enabled: true }, 'wrong first prompt')));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    expect(screen.queryByDisplayValue('wrong first prompt')).toBeNull();
    expect(screen.queryByDisplayValue('wrong second prompt')).toBeNull();
    expect(getVibeAgent).toHaveBeenNthCalledWith(2, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    fireEvent.click(screen.getByText('agents.detail.systemPrompt').closest('button')!);
    expect(screen.getByDisplayValue('base prompt')).toBeTruthy();

    act(() => drain.resolve(fullAgent({ ...initial, description: 'final description', enabled: false }, 'final prompt')));
    await waitFor(() => expect(screen.getByDisplayValue('local description')).toBeTruthy());
    await waitFor(() => expect(screen.getByDisplayValue('final prompt')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenCalledTimes(2);
    expect(api.connectWorkbenchEvents).toHaveBeenCalledTimes(1);
  });

  it('lets the post-batch read win across an A -> B -> A selection race', async () => {
    const agentA = { ...brief('agent-a', 'A'), model: 'base-model' };
    const agentB = brief('agent-b', 'B');
    const patch = deferred<ReturnType<typeof fullAgent>>();
    const oldB = deferred<ReturnType<typeof fullAgent>>();
    const oldA = deferred<ReturnType<typeof fullAgent>>();
    const drain = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(oldB.promise)
      .mockReturnValueOnce(oldA.promise)
      .mockReturnValueOnce(drain.promise);
    const updateVibeAgent = vi.fn().mockReturnValue(patch.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult([agentA, agentB])), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A'), { target: { value: 'local A' } });
    fireEvent.blur(screen.getByDisplayValue('local A'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    const rowA = screen.getAllByText('agent-a').find((node) => node.closest('button'))?.closest('button');
    expect(rowA).not.toBeNull();
    fireEvent.click(rowA!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));

    act(() => patch.resolve(fullAgent({ ...agentA, description: 'wrong patch response' }, 'wrong patch prompt')));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(4));
    act(() => oldB.resolve(fullAgent(agentB, 'old B')));
    act(() => oldA.resolve(fullAgent({ ...agentA, description: 'old A' }, 'old A')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByDisplayValue('old A')).toBeNull();

    act(() => drain.resolve(fullAgent({ ...agentA, description: 'final A' }, 'final prompt')));
    await waitFor(() => expect(screen.getByText('agents.detail.systemPrompt').closest('button')).toBeTruthy());
    fireEvent.click(screen.getByText('agents.detail.systemPrompt').closest('button')!);
    await waitFor(() => expect(screen.getByDisplayValue('final prompt')).toBeTruthy());
    expect(screen.getByDisplayValue('final A')).toBeTruthy();
    expect(getVibeAgent).toHaveBeenCalledTimes(4);
  });

  it('preserves dirty drafts while clean detail fields adopt a same-agent snapshot', async () => {
    const initial = { ...brief('agent-a', 'clean description'), model: 'old-model' };
    const changed = { ...initial, description: 'server description', model: 'new-model' };
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'clean prompt'))
      .mockResolvedValueOnce(fullAgent(changed, 'server prompt'));
    const api = makeApi(
      vi.fn()
        .mockResolvedValueOnce(listResult(initial))
        .mockResolvedValueOnce(listResult(changed)),
      getVibeAgent,
    );
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('clean description')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('clean description'), { target: { value: 'dirty description' } });
    const promptToggle = screen.getByText('agents.detail.systemPrompt').closest('button');
    expect(promptToggle).not.toBeNull();
    fireEvent.click(promptToggle!);
    const inlinePrompt = screen.getByPlaceholderText('agents.create.systemPromptPlaceholder');
    fireEvent.change(inlinePrompt, { target: { value: 'dirty inline prompt' } });

    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    const modalPrompt = screen.getAllByPlaceholderText('agents.create.systemPromptPlaceholder').at(-1)!;
    fireEvent.change(modalPrompt, { target: { value: 'dirty modal prompt' } });

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByRole('combobox', { hidden: true }).textContent).toContain('new-model'));
    expect(screen.getByDisplayValue('dirty description')).toBeTruthy();
    expect(screen.getByDisplayValue('dirty inline prompt')).toBeTruthy();
    expect(screen.getByDisplayValue('dirty modal prompt')).toBeTruthy();
    expect(screen.getByText('agents.detail.systemPromptEditorHint')).toBeTruthy();
  });

  it('canonicalizes trimmed description and inline prompt saves', async () => {
    const initial = brief('agent-a', 'before');
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'old prompt'))
      .mockResolvedValueOnce(fullAgent({ ...initial, description: 'canonical description' }, 'old prompt'))
      .mockResolvedValueOnce(fullAgent({ ...initial, description: 'canonical description' }, 'canonical prompt'))
      .mockResolvedValueOnce(fullAgent({ ...initial, description: 'external description' }, 'external prompt'));
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true, agent: fullAgent(initial, 'ack').agent });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('before')).toBeTruthy());
    const description = screen.getByDisplayValue('before');
    fireEvent.change(description, { target: { value: '  canonical description  ' } });
    fireEvent.blur(description);
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { description: 'canonical description' }));
    expect(screen.getByDisplayValue('canonical description')).toBeTruthy();
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getByText('agents.detail.systemPrompt').closest('button')!);
    const inlinePrompt = screen.getByPlaceholderText('agents.create.systemPromptPlaceholder');
    fireEvent.change(inlinePrompt, { target: { value: '  canonical prompt  ' } });
    fireEvent.blur(inlinePrompt);
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { system_prompt: 'canonical prompt' }));
    expect(screen.getByDisplayValue('canonical prompt')).toBeTruthy();
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue('external description')).toBeTruthy());
    expect(screen.getByDisplayValue('external description')).toBeTruthy();
  });

  it.each([
    { field: 'name', initial: 'agent-a', draft: 'agent-a ', submitted: 'agent-a continued', patch: { name: 'agent-a continued' } },
    { field: 'description', initial: 'description', draft: 'description ', submitted: 'description continued', patch: { description: 'description continued' } },
    { field: 'systemPrompt', initial: 'system prompt', draft: 'system prompt ', submitted: 'system prompt continued', patch: { system_prompt: 'system prompt continued' } },
  ] as const)('preserves an active raw $field draft across same-agent reconciliation', async ({ field, initial, draft, submitted, patch }) => {
    const initialAgent = brief('agent-a', field === 'description' ? initial : 'description');
    const changedAgent = { ...initialAgent, model: 'reconciled-model' };
    const finalAgent = {
      ...changedAgent,
      ...(field === 'name' ? { name: submitted, display_name: submitted } : {}),
      ...(field === 'description' ? { description: submitted } : {}),
    };
    let readCount = 0;
    const getVibeAgent = vi.fn((requestedName: string) => {
      readCount += 1;
      if (readCount === 1) return Promise.resolve(fullAgent(initialAgent, field === 'systemPrompt' ? initial : 'prompt'));
      if (readCount === 2) return Promise.resolve(fullAgent(changedAgent, field === 'systemPrompt' ? initial : 'prompt'));
      return Promise.resolve(fullAgent({ ...finalAgent, name: field === 'name' ? requestedName : finalAgent.name }, submitted));
    });
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initialAgent)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    let editedInput: HTMLInputElement | HTMLTextAreaElement;
    if (field === 'systemPrompt') {
      await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
      fireEvent.click(screen.getByText('agents.detail.systemPrompt').closest('button')!);
      editedInput = screen.getByPlaceholderText('agents.create.systemPromptPlaceholder');
      fireEvent.change(editedInput, { target: { value: draft } });
    } else {
      await waitFor(() => expect(screen.getByDisplayValue(initial)).toBeTruthy());
      editedInput = screen.getByDisplayValue(initial);
      fireEvent.change(editedInput, { target: { value: draft } });
    }

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    expect(editedInput.value).toBe(draft);

    fireEvent.change(editedInput, { target: { value: submitted } });
    fireEvent.blur(editedInput);
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', patch));
  });

  it.each([
    { field: 'description', label: 'description' },
    { field: 'systemPrompt', label: 'inline system prompt' },
  ] as const)('adopts a newer server snapshot after a $label edit is restored to baseline', async ({ field }) => {
    const initial = brief('agent-a', 'before');
    const changed = { ...initial, description: 'server description' };
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'before prompt'))
      .mockResolvedValueOnce(fullAgent(changed, 'server prompt'));
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('before')).toBeTruthy());
    if (field === 'description') {
      const input = screen.getByDisplayValue('before');
      fireEvent.change(input, { target: { value: 'local description' } });
      fireEvent.change(input, { target: { value: 'before' } });
      fireEvent.blur(input);
      expect(api.updateVibeAgent).not.toHaveBeenCalled();
    } else {
      fireEvent.click(screen.getByRole('button', { name: /agents\.detail\.systemPromptCount/ }));
      const input = screen.getByDisplayValue('before prompt');
      fireEvent.change(input, { target: { value: 'local prompt' } });
      fireEvent.change(input, { target: { value: 'before prompt' } });
      fireEvent.blur(input);
      expect(api.updateVibeAgent).not.toHaveBeenCalled();
    }

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue(field === 'description' ? 'server description' : 'server prompt')).toBeTruthy());
  });

  it('restores the authoritative name on Escape and does not rename on blur', async () => {
    const initial = brief('agent-a', 'description');
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'prompt'))
      .mockResolvedValueOnce(fullAgent({ ...initial, description: 'after gap' }, 'prompt'));
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('agent-a')).toBeTruthy());
    const input = screen.getByDisplayValue('agent-a');
    fireEvent.change(input, { target: { value: 'local-name' } });
    fireEvent.keyDown(input, { key: 'Escape' });
    fireEvent.blur(input);
    expect(screen.getByDisplayValue('agent-a')).toBeTruthy();
    expect(api.updateVibeAgent).not.toHaveBeenCalled();

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue('after gap')).toBeTruthy());
    expect(api.updateVibeAgent).not.toHaveBeenCalled();
  });

  it.each([
    { label: 'rejected PATCH', patch: 'reject' as const },
    { label: 'ok-false PATCH', patch: 'okfalse' as const },
    { label: 'failed authoritative drain', patch: 'drain' as const },
  ])('keeps the prompt editor draft open after a $label and closes after retry', async ({ patch }) => {
    const initial = brief('agent-a', 'description');
    const getVibeAgent = vi.fn().mockResolvedValueOnce(fullAgent(initial, 'server prompt'));
    const updateVibeAgent = vi.fn();
    if (patch === 'drain') {
      getVibeAgent.mockRejectedValueOnce({ message: 'drain failed' });
      updateVibeAgent.mockResolvedValueOnce(fullAgent(initial, 'acknowledged'));
    } else {
      getVibeAgent.mockResolvedValueOnce(fullAgent(initial, 'server prompt'));
      if (patch === 'reject') updateVibeAgent.mockRejectedValueOnce({ message: 'save failed' });
      else updateVibeAgent.mockResolvedValueOnce({ ok: false, message: 'save failed' });
    }
    getVibeAgent.mockResolvedValueOnce(fullAgent({ ...initial, description: 'saved' }, 'saved prompt'));
    updateVibeAgent.mockResolvedValueOnce(fullAgent({ ...initial, description: 'saved' }, 'saved prompt'));
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    const draft = screen.getByPlaceholderText('agents.create.systemPromptPlaceholder');
    fireEvent.change(draft, { target: { value: 'failed private draft' } });
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));

    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByDisplayValue('failed private draft')).toBeTruthy());
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(document.body.textContent).toContain(patch === 'drain' ? 'drain failed' : 'save failed');

    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    fireEvent.click(screen.getByRole('button', { name: /agents\.detail\.systemPromptCount/ }));
    expect(screen.getByDisplayValue('saved prompt')).toBeTruthy();
  });

  it('lets a newer reconnect publish satisfy a successful prompt save drain', async () => {
    const agent = brief('agent-a', 'description');
    const firstDrain = deferred<ReturnType<typeof fullAgent>>();
    const reconnect = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial prompt'))
      .mockReturnValueOnce(firstDrain.promise)
      .mockReturnValueOnce(reconnect.promise);
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    fireEvent.change(screen.getByPlaceholderText('agents.create.systemPromptPlaceholder'), {
      target: { value: 'saved prompt' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    act(() => reconnect.resolve(fullAgent(agent, 'saved prompt')));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.queryByText('Agent reconciliation failed')).toBeNull();
    act(() => firstDrain.resolve(fullAgent(agent, 'stale drain')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(updateVibeAgent).toHaveBeenCalledTimes(1);
  });

  it('keeps the selected catch-up barrier alive when a manual list refresh supersedes it', async () => {
    const agent = brief('agent-a', 'before');
    const gapList = deferred<ReturnType<typeof listResult>>();
    const manualList = deferred<ReturnType<typeof listResult>>();
    const detail = deferred<ReturnType<typeof fullAgent>>();
    const refreshed = { ...agent, description: 'after gap' };
    const listVibeAgents = vi.fn()
      .mockResolvedValueOnce(listResult(agent))
      .mockReturnValueOnce(gapList.promise)
      .mockReturnValueOnce(manualList.promise);
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial'))
      .mockReturnValueOnce(detail.promise);
    const api = makeApi(listVibeAgents, getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('before')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(listVibeAgents).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: 'common.refresh' }));
    await waitFor(() => expect(listVibeAgents).toHaveBeenCalledTimes(3));

    act(() => manualList.resolve(listResult(refreshed)));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    expect(getVibeAgent).toHaveBeenNthCalledWith(2, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    act(() => detail.resolve(fullAgent(refreshed, 'reconciled')));
    await waitFor(() => expect(screen.getByDisplayValue('after gap')).toBeTruthy());

    act(() => gapList.resolve(listResult(agent)));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('after gap')).toBeTruthy();
    expect(getVibeAgent).toHaveBeenCalledTimes(2);
  });

  it('publishes the Definitions list only after the authoritative mutation detail drain', async () => {
    const agent = brief('agent-a', 'before');
    const preMutationList = deferred<ReturnType<typeof listResult>>();
    const postDrainList = deferred<ReturnType<typeof listResult>>();
    const drain = deferred<ReturnType<typeof fullAgent>>();
    const finalAgent = { ...agent, description: 'after mutation' };
    const listVibeAgents = vi.fn()
      .mockResolvedValueOnce(listResult(agent))
      .mockReturnValueOnce(preMutationList.promise)
      .mockReturnValueOnce(postDrainList.promise);
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial'))
      .mockReturnValueOnce(drain.promise)
      .mockResolvedValue(fullAgent(finalAgent, 'final'));
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(listVibeAgents, getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('before')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(listVibeAgents).toHaveBeenCalledTimes(2));
    fireEvent.change(screen.getByDisplayValue('before'), { target: { value: 'local' } });
    fireEvent.blur(screen.getByDisplayValue('local'));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    act(() => drain.resolve(fullAgent(finalAgent, 'final')));
    await waitFor(() => expect(listVibeAgents).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    expect(screen.getByDisplayValue('final')).toBeTruthy();
    act(() => postDrainList.resolve(listResult(finalAgent)));
    await waitFor(() => expect(screen.getAllByText('after mutation').length).toBeGreaterThan(0));

    act(() => preMutationList.resolve(listResult(agent)));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getAllByText('after mutation').length).toBeGreaterThan(0);
  });

  it('keeps per-operation mutation outcomes independent within one detail barrier', async () => {
    const agent = brief('agent-a', 'before');
    const descriptionPatch = deferred<unknown>();
    const promptPatch = deferred<unknown>();
    const drain = deferred<ReturnType<typeof fullAgent>>();
    const finalAgent = { ...agent, description: 'server description' };
    const updateVibeAgent = vi.fn()
      .mockReturnValueOnce(descriptionPatch.promise)
      .mockReturnValueOnce(promptPatch.promise);
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial prompt'))
      .mockReturnValueOnce(drain.promise);
    const api = makeApi(
      vi.fn().mockResolvedValue(listResult(finalAgent)),
      getVibeAgent,
      undefined,
      undefined,
      updateVibeAgent,
    );
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('before')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('before'), { target: { value: 'local description' } });
    fireEvent.blur(screen.getByDisplayValue('local description'));
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    fireEvent.change(screen.getByPlaceholderText('agents.create.systemPromptPlaceholder'), {
      target: { value: 'saved prompt' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(2));

    act(() => promptPatch.resolve({ ok: true }));
    act(() => descriptionPatch.reject({ message: 'description failed' }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => drain.resolve(fullAgent(finalAgent, 'saved prompt')));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByText('description failed')).toBeTruthy();
    expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { system_prompt: 'saved prompt' });
  });

  it('clears the reasoning effort in the same patch as a model that has none', async () => {
    // The record must never keep an effort the selected model cannot run, and
    // "no efforts at all" is the case a fallback-shaped fix misses: there is no
    // valid value to swap in, so the field has to be cleared instead.
    modelCatalog = { models: [], reasoningOptions: { 'no-effort-model': [] } };
    const initial = {
      ...brief('agent-a', 'description'),
      model: 'old-model',
      reasoning_effort: 'medium' as const,
    };
    // What the server holds once the patch lands: the new model, and no effort.
    const cleared = fullAgent({ ...initial, model: 'no-effort-model' }, 'initial prompt');
    cleared.agent.reasoning_effort = null;
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'initial prompt'))
      .mockResolvedValue(cleared);
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    expect(screen.getByRole('button', { name: 'medium', exact: true }).className).toContain('bg-mint-soft');

    fireEvent.click(screen.getByRole('combobox'));
    fireEvent.change(screen.getByPlaceholderText('Search...'), { target: { value: 'no-effort-model' } });
    fireEvent.click(screen.getByText('Use "no-effort-model"'));

    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', {
      model: 'no-effort-model',
      reasoning_effort: null,
    }));
    expect(updateVibeAgent).toHaveBeenCalledTimes(1);
    // The whole field goes, not just its segments: an empty outline under a
    // heading reads as a control that failed to load.
    expect(screen.queryByRole('button', { name: 'medium', exact: true })).toBeNull();
    expect(screen.queryByText('agents.detail.effort')).toBeNull();
  });

  it('keeps an unset server effort unset instead of lighting a default', async () => {
    // The reported inconsistency: the panel showed `medium` for an agent the
    // server held at null, so switching models sent only `model` and the record
    // stayed effort-less while the UI claimed otherwise.
    modelCatalog = {
      models: [],
      reasoningOptions: {
        'no-effort-model': [],
        'reasoning-model': [{ value: 'medium', label: 'Medium' }],
      },
    };
    const initial = {
      ...brief('agent-a', 'description'),
      model: 'no-effort-model',
      reasoning_effort: null,
    };
    const full = fullAgent(initial, 'initial prompt');
    full.agent.reasoning_effort = null;
    // What the server holds after the patch: the new model, and still no effort.
    const switched = fullAgent({ ...initial, model: 'reasoning-model' }, 'initial prompt');
    switched.agent.reasoning_effort = null;
    const getVibeAgent = vi.fn().mockResolvedValueOnce(full).mockResolvedValue(switched);
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    expect(screen.queryByText('agents.detail.effort')).toBeNull();

    fireEvent.click(screen.getByRole('combobox'));
    fireEvent.change(screen.getByPlaceholderText('Search...'), { target: { value: 'reasoning-model' } });
    fireEvent.click(screen.getByText('Use "reasoning-model"'));

    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { model: 'reasoning-model' }));
    // The column is back because this model has one, but no segment is chosen:
    // the server holds no effort, and the panel may not invent one.
    expect(screen.getByText('agents.detail.effort')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'medium', exact: true }).className).not.toContain('bg-mint-soft');
  });

  it('keeps a still-valid effort untouched when the model changes', async () => {
    modelCatalog = {
      models: [],
      reasoningOptions: { 'reasoning-model': [{ value: 'medium', label: 'Medium' }] },
    };
    const initial = {
      ...brief('agent-a', 'description'),
      model: 'old-model',
      reasoning_effort: 'medium' as const,
    };
    const getVibeAgent = vi.fn().mockResolvedValue(fullAgent(initial, 'initial prompt'));
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    fireEvent.click(screen.getByRole('combobox'));
    fireEvent.change(screen.getByPlaceholderText('Search...'), { target: { value: 'reasoning-model' } });
    fireEvent.click(screen.getByText('Use "reasoning-model"'));

    // A model switch is not an effort edit: only the field the user changed goes.
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { model: 'reasoning-model' }));
    // A model that has an effort keeps the field, still showing the current one.
    expect(screen.getByText('agents.detail.effort')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'medium', exact: true }).className).toContain('bg-mint-soft');
  });

  it.each(['model', 'effort'] as const)('restores the authoritative value after a failed %s mutation', async (field) => {
    const initial = {
      ...brief('agent-a', 'description'),
      model: 'old-model',
      reasoning_effort: 'medium' as const,
    };
    const update = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'initial prompt'))
      .mockResolvedValueOnce(fullAgent(initial, 'authoritative prompt'));
    const updateVibeAgent = vi.fn().mockReturnValue(update.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    if (field === 'model') {
      fireEvent.click(screen.getByRole('combobox'));
      fireEvent.change(screen.getByPlaceholderText('Search...'), { target: { value: 'new-model' } });
      fireEvent.click(screen.getByText('Use "new-model"'));
      await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { model: 'new-model' }));
    } else {
      fireEvent.click(screen.getByRole('button', { name: 'high', exact: true }));
      await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { reasoning_effort: 'high' }));
    }

    act(() => update.reject({ message: `${field} failed` }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    if (field === 'model') {
      await waitFor(() => expect(screen.getByRole('combobox', { hidden: true }).textContent).toContain('old-model'));
    } else {
      await waitFor(() => expect(screen.getByRole('button', { name: 'medium', exact: true }).className).toContain('bg-mint-soft'));
    }
    expect(screen.queryByText(`${field} failed`)).toBeTruthy();
  });

  it.each([
    { label: 'underlying message', failure: { message: 'authoritative drain failed' }, expected: 'authoritative drain failed' },
    { label: 'message-less fallback', failure: {}, expected: 'errorBoundary.title' },
  ])('surfaces the $label from a current mutation drain', async ({ failure, expected }) => {
    const agent = brief('agent-a', 'before');
    const drain = deferred<ReturnType<typeof fullAgent>>();
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'prompt'))
      .mockReturnValueOnce(drain.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('before')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('before'), { target: { value: 'submitted' } });
    fireEvent.blur(screen.getByDisplayValue('submitted'));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => drain.reject(failure));
    await waitFor(() => expect(screen.getByText(expected)).toBeTruthy());
    expect(screen.queryByText('Agent reconciliation failed')).toBeNull();
  });

  it.each([
    { label: 'transport rejection', update: 'reject' as const, message: 'enabled transport failed' },
    { label: 'HTTP ok false', update: 'okfalse' as const, message: 'enabled server rejected' },
    { label: 'authoritative drain failure', update: 'drain' as const, message: 'enabled drain failed' },
  ])('consumes enabled background failures ($label)', async ({ update, message }) => {
    const agent = brief('agent-a', 'description');
    const getVibeAgent = vi.fn().mockResolvedValueOnce(fullAgent(agent, 'prompt'));
    const updateVibeAgent = vi.fn();
    if (update === 'reject') {
      updateVibeAgent.mockRejectedValueOnce({ message });
      getVibeAgent.mockResolvedValue(fullAgent(agent, 'prompt'));
    } else if (update === 'okfalse') {
      updateVibeAgent.mockResolvedValueOnce({ ok: false, message });
      getVibeAgent.mockResolvedValue(fullAgent(agent, 'prompt'));
    } else {
      updateVibeAgent.mockResolvedValueOnce({ ok: true });
      getVibeAgent.mockRejectedValueOnce({ message });
      getVibeAgent.mockResolvedValue(fullAgent(agent, 'prompt'));
    }
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    const toggle = screen.getByRole('switch', { name: 'agents.detail.enabled' });
    fireEvent.click(toggle);
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { enabled: false }));
    await waitFor(() => expect(screen.getByText(message)).toBeTruthy());
    expect(toggle.getAttribute('aria-checked')).toBe('true');
  });

  it('preserves a newer model edit when an older mutation drain settles', async () => {
    const initial = { ...brief('agent-a', 'description'), model: 'old-model', reasoning_effort: 'medium' as const };
    const firstPatch = deferred<ReturnType<typeof fullAgent>>();
    const secondPatch = deferred<ReturnType<typeof fullAgent>>();
    const firstDrain = deferred<ReturnType<typeof fullAgent>>();
    const secondDrain = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'initial prompt'))
      .mockReturnValueOnce(firstDrain.promise)
      .mockReturnValueOnce(secondDrain.promise);
    const updateVibeAgent = vi.fn()
      .mockReturnValueOnce(firstPatch.promise)
      .mockReturnValueOnce(secondPatch.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    const chooseModel = (value: string) => {
      fireEvent.click(screen.getByRole('combobox'));
      fireEvent.change(screen.getByPlaceholderText('Search...'), { target: { value } });
      fireEvent.click(screen.getByText(`Use "${value}"`));
    };
    chooseModel('model-one');
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { model: 'model-one' }));
    act(() => firstPatch.resolve(fullAgent({ ...initial, model: 'model-one' }, 'first acknowledgement')));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    chooseModel('model-two');
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-a', { model: 'model-two' }));
    act(() => firstDrain.resolve(fullAgent({ ...initial, model: 'model-one' }, 'first drain')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole('combobox', { hidden: true }).textContent).toContain('model-two');

    act(() => secondPatch.reject({ message: 'second failed' }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    act(() => secondDrain.resolve(fullAgent(initial, 'final authoritative')));
    await waitFor(() => expect(screen.getByRole('combobox', { hidden: true }).textContent).toContain('old-model'));
  });

  it('reseeds an untouched open prompt editor but preserves an edited modal draft', async () => {
    const initial = brief('agent-a', 'description');
    const changed = { ...initial, description: 'description', model: 'new-model' };
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(initial, 'old prompt'))
      .mockResolvedValueOnce(fullAgent(changed, 'new prompt'))
      .mockResolvedValueOnce(fullAgent({ ...changed, description: 'description 2' }, 'latest prompt'))
      .mockResolvedValueOnce(fullAgent({ ...changed, description: 'description 3' }, 'reconciled prompt'));
    const api = makeApi(vi.fn().mockResolvedValue(listResult(initial)), getVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    await waitFor(() => expect(screen.getAllByDisplayValue('old prompt').length).toBeGreaterThan(0));
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue('new prompt')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'common.save' }));
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    await waitFor(() => expect(screen.getByDisplayValue('new prompt')).toBeTruthy());

    const modalPrompt = screen.getAllByPlaceholderText('agents.create.systemPromptPlaceholder').at(-1)!;
    fireEvent.change(modalPrompt, { target: { value: 'edited modal draft' } });
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue('edited modal draft')).toBeTruthy());
  });

  it('orders onboarding reads around a mutation and ignores stale results', async () => {
    const agent = brief('agent-a', 'agent description');
    const initial = deferred<ReturnType<typeof onboardingResult>>();
    const reconnect = deferred<ReturnType<typeof onboardingResult>>();
    const gapAfterMutation = deferred<ReturnType<typeof onboardingResult>>();
    const settlement = deferred<ReturnType<typeof onboardingResult>>();
    const onboardingResult = (notOnboarded: number, privateCount: number) => ({
      ok: true,
      available: true,
      organization_id: 'org-1',
      agents: [],
      counts: { total: 1, system: 0, custom: 1, not_onboarded: notOnboarded, private: privateCount, published: 0, conflicts: 0 },
    });
    const getVibeAgentOnboarding = vi.fn()
      .mockReturnValueOnce(initial.promise)
      .mockReturnValueOnce(reconnect.promise)
      .mockReturnValueOnce(gapAfterMutation.promise)
      .mockReturnValueOnce(settlement.promise);
    const post = deferred<ReturnType<typeof onboardingResult>>();
    const api = makeApi(
      vi.fn().mockResolvedValue(listResult(agent)),
      vi.fn().mockResolvedValue({ ok: false }),
      getVibeAgentOnboarding,
      vi.fn().mockReturnValue(post.promise),
    );
    renderPage(api, { canManageAgents: true });
    await waitFor(() => expect(handlers).not.toBeNull());

    act(() => handlers?.onConnected?.());
    act(() => reconnect.resolve(onboardingResult(1, 0)));
    await waitFor(() => expect(screen.getByText('agents.onboarding.privateCount:{"count":0}')).toBeTruthy());
    act(() => initial.resolve(onboardingResult(1, 1)));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText('agents.onboarding.privateCount:{"count":0}')).toBeTruthy();

    fireEvent.click(screen.getByText('agents.onboarding.onboardPrivate'));
    await waitFor(() => expect(api.onboardVibeAgents).toHaveBeenCalledTimes(1));
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgentOnboarding).toHaveBeenCalledTimes(3));
    act(() => gapAfterMutation.resolve(onboardingResult(0, 3)));
    await waitFor(() => expect(screen.getByText('agents.onboarding.privateCount:{"count":3}')).toBeTruthy());
    act(() => post.resolve(onboardingResult(0, 99)));
    act(() => settlement.resolve(onboardingResult(1, 2)));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getByText('agents.onboarding.notOnboardedCount:{"count":1}')).toBeTruthy());
    expect(screen.getByText('agents.onboarding.privateCount:{"count":2}')).toBeTruthy();
    expect(getVibeAgentOnboarding).toHaveBeenCalledTimes(4);
  });

  it('keeps a newer reconnect onboarding snapshot over a settled mutation read', async () => {
    const agent = brief('agent-a', 'agent description');
    const settlement = deferred<{ available: boolean; counts: Record<string, number> }>();
    const reconnect = deferred<{ available: boolean; counts: Record<string, number> }>();
    const result = (privateCount: number) => ({
      ok: true,
      available: true,
      organization_id: 'org-1',
      agents: [],
      counts: { total: 1, system: 0, custom: 1, not_onboarded: 1, private: privateCount, published: 0, conflicts: 0 },
    });
    const getVibeAgentOnboarding = vi.fn()
      .mockResolvedValueOnce(result(0))
      .mockReturnValueOnce(settlement.promise)
      .mockReturnValueOnce(reconnect.promise);
    const post = vi.fn().mockResolvedValue(result(99));
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), vi.fn().mockResolvedValue({ ok: false }), getVibeAgentOnboarding, post);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByText('agents.onboarding.privateCount:{"count":0}')).toBeTruthy());
    fireEvent.click(screen.getByText('agents.onboarding.onboardPrivate'));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgentOnboarding).toHaveBeenCalledTimes(3));
    act(() => settlement.resolve(result(1)));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText('agents.onboarding.privateCount:{"count":0}')).toBeTruthy();
    act(() => reconnect.resolve(result(2)));
    await waitFor(() => expect(screen.getByText('agents.onboarding.privateCount:{"count":2}')).toBeTruthy());
    expect(screen.queryByText('agents.onboarding.privateCount:{"count":99}')).toBeNull();
  });

  it('keeps onboarding submission locked until its settlement inventory publishes', async () => {
    const agent = brief('agent-a', 'agent description');
    const settlement = deferred<ReturnType<typeof onboardingResult>>();
    const onboardingResult = (notOnboarded: number) => ({
      ok: true,
      available: true,
      organization_id: 'org-1',
      agents: [],
      counts: { total: 1, system: 0, custom: 1, not_onboarded: notOnboarded, private: 0, published: 0, conflicts: 0 },
    });
    const getVibeAgentOnboarding = vi.fn()
      .mockResolvedValueOnce(onboardingResult(1))
      .mockReturnValueOnce(settlement.promise);
    const onboardVibeAgents = vi.fn().mockResolvedValue({ ok: true, created: 1 });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), vi.fn().mockResolvedValue(fullAgent(agent, 'prompt')), getVibeAgentOnboarding, onboardVibeAgents);
    renderPage(api, { canManageAgents: true });

    const button = await screen.findByRole('button', { name: 'agents.onboarding.onboardPrivate' });
    fireEvent.click(button);
    await waitFor(() => expect(onboardVibeAgents).toHaveBeenCalledTimes(1));
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(button);
    expect(onboardVibeAgents).toHaveBeenCalledTimes(1);

    act(() => settlement.resolve(onboardingResult(0)));
    await waitFor(() => expect((screen.getByRole('button', { name: 'agents.onboarding.onboarded' }) as HTMLButtonElement).disabled).toBe(true));
  });

  it('does not re-enable stale onboarding inventory after settlement failure', async () => {
    const agent = brief('agent-a', 'agent description');
    const settlement = deferred<ReturnType<typeof onboardingResult>>();
    const onboardingResult = (notOnboarded: number) => ({
      ok: true,
      available: true,
      organization_id: 'org-1',
      agents: [],
      counts: { total: 1, system: 0, custom: 1, not_onboarded: notOnboarded, private: 0, published: 0, conflicts: 0 },
    });
    const getVibeAgentOnboarding = vi.fn()
      .mockResolvedValueOnce(onboardingResult(1))
      .mockReturnValueOnce(settlement.promise);
    const onboardVibeAgents = vi.fn().mockResolvedValue({ ok: true, created: 1 });
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), vi.fn().mockResolvedValue(fullAgent(agent, 'prompt')), getVibeAgentOnboarding, onboardVibeAgents);
    renderPage(api, { canManageAgents: true });

    const button = await screen.findByRole('button', { name: 'agents.onboarding.onboardPrivate' });
    fireEvent.click(button);
    await waitFor(() => expect(onboardVibeAgents).toHaveBeenCalledTimes(1));
    act(() => settlement.reject(new Error('inventory unavailable')));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'agents.onboarding.onboardPrivate' })).toBeNull());
    expect(onboardVibeAgents).toHaveBeenCalledTimes(1);
  });

  it('does not drain a non-selected mutation and retires it before returning to that agent', async () => {
    const agentA = { ...brief('agent-a', 'A'), model: 'a-model' };
    const agentB = brief('agent-b', 'B');
    const pendingB = deferred<ReturnType<typeof fullAgent>>();
    const finalA = fullAgent({ ...agentA, description: 'A after return' }, 'A after return');
    const patch = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(pendingB.promise)
      .mockResolvedValueOnce(finalA);
    const listVibeAgents = vi.fn().mockResolvedValue(listResult([agentA, agentB]));
    const updateVibeAgent = vi.fn().mockReturnValue(patch.promise);
    const api = makeApi(listVibeAgents, getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A'), { target: { value: 'A local' } });
    fireEvent.blur(screen.getByDisplayValue('A local'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => patch.resolve(fullAgent({ ...agentA, description: 'A local' }, 'A ack')));
    await waitFor(() => expect(listVibeAgents).toHaveBeenCalledTimes(2));
    expect(getVibeAgent).toHaveBeenCalledTimes(2);

    act(() => pendingB.resolve(fullAgent(agentB, 'B selected')));
    await waitFor(() => expect(screen.getByDisplayValue('B')).toBeTruthy());
    const rowA = screen.getAllByText('agent-a').find((node) => node.closest('button'))?.closest('button');
    expect(rowA).not.toBeNull();
    fireEvent.click(rowA!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.getByDisplayValue('A after return')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
  });

  it.each([
    { label: 'thrown failure', reject: true },
    { label: 'HTTP ok false', reject: false },
  ])('keeps a non-selected mutation $label visible after Definitions refresh', async ({ reject }) => {
    const agentA = brief('agent-a', 'A');
    const agentB = brief('agent-b', 'B');
    const pendingB = deferred<ReturnType<typeof fullAgent>>();
    const patch = deferred<unknown>();
    const finalA = fullAgent({ ...agentA, description: 'A after failed return' }, 'A after failed return');
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(pendingB.promise)
      .mockResolvedValueOnce(finalA);
    const updateVibeAgent = vi.fn().mockReturnValue(patch.promise);
    const api = makeApi(
      vi.fn().mockResolvedValue(listResult([agentA, agentB])),
      getVibeAgent,
      undefined,
      undefined,
      updateVibeAgent,
    );
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A'), { target: { value: 'A local' } });
    fireEvent.blur(screen.getByDisplayValue('A local'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    if (reject) {
      act(() => patch.reject({ message: 'A mutation failed' }));
    } else {
      act(() => patch.resolve({ ok: false, message: 'A mutation failed' }));
    }
    await waitFor(() => expect(screen.getByText('A mutation failed')).toBeTruthy());
    act(() => pendingB.resolve(fullAgent(agentB, 'B selected')));
    await waitFor(() => expect(screen.getByDisplayValue('B')).toBeTruthy());
    expect(screen.getByText('A mutation failed')).toBeTruthy();
    fireEvent.click(screen.getByText('agent-a').closest('button')!);
    await waitFor(() => expect(screen.getByDisplayValue('A after failed return')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
  });

  it.each([
    { label: 'thrown failure', reject: true },
    { label: 'HTTP ok false', reject: false },
  ])('drains a settled mutation after selection rollback for $label', async ({ reject }) => {
    const agentA = { ...brief('agent-a', 'A'), model: 'a-model' };
    const agentB = brief('agent-b', 'B');
    const pendingB = deferred<ReturnType<typeof fullAgent>>();
    const patch = deferred<unknown>();
    const rollbackA = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(pendingB.promise)
      .mockReturnValueOnce(rollbackA.promise);
    const updateVibeAgent = vi.fn().mockReturnValue(patch.promise);
    const api = makeApi(
      vi.fn().mockResolvedValue(listResult([agentA, agentB])),
      getVibeAgent,
      undefined,
      undefined,
      updateVibeAgent,
    );
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A'), { target: { value: 'A local' } });
    fireEvent.blur(screen.getByDisplayValue('A local'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    if (reject) act(() => patch.reject({ message: 'A mutation failed' }));
    else act(() => patch.resolve({ ok: false, message: 'A mutation failed' }));
    await waitFor(() => expect(screen.getByText('A mutation failed')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenCalledTimes(2);

    act(() => pendingB.reject({ code: 'agent_backend_unavailable', message: 'B unavailable' }));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    act(() => rollbackA.resolve(fullAgent({ ...agentA, description: 'A after patch' }, 'A reconciled')));
    await waitFor(() => expect(screen.getByDisplayValue('A after patch')).toBeTruthy());
    fireEvent.click(screen.getByText('agents.detail.systemPrompt').closest('button')!);
    expect(screen.getByDisplayValue('A reconciled')).toBeTruthy();
    expect(screen.getByText('A mutation failed')).toBeTruthy();
  });

  it.each([
    { label: 'rejected', reject: true, code: 'agent_not_found' },
    { label: 'ok-false', reject: false, code: 'agent_access_forbidden' },
  ])('drains accepted-A debt after pending B disappears on the same edge ($label)', async ({ reject, code }) => {
    const agentA = { ...brief('agent-a', 'A before'), model: 'a-model' };
    const agentB = brief('agent-b', 'B');
    const pendingB = deferred<ReturnType<typeof fullAgent>>();
    const patch = deferred<unknown>();
    const catchupB = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(pendingB.promise)
      .mockReturnValueOnce(catchupB.promise)
      .mockResolvedValueOnce(fullAgent({ ...agentA, description: 'A after debt' }, 'A reconciled'));
    const updateVibeAgent = vi.fn().mockReturnValue(patch.promise);
    const api = makeApi(
      vi.fn().mockResolvedValue(listResult([agentA, agentB])),
      getVibeAgent,
      undefined,
      undefined,
      updateVibeAgent,
    );
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A before')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A before'), { target: { value: 'A local' } });
    fireEvent.blur(screen.getByDisplayValue('A local'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    act(() => patch.resolve({ ok: true }));
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    if (reject) act(() => catchupB.reject({ code, message: 'B disappeared' }));
    else act(() => catchupB.resolve({ ok: false, code, message: 'B disappeared' } as never));

    await waitFor(() => expect(screen.getByDisplayValue('A after debt')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenNthCalledWith(4, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(screen.queryByText('agent-b')).toBeNull();
  });

  it.each([
    { label: 'HTTP ok false', rejected: false, result: { ok: false, message: 'delete rejected' } },
    { label: 'transport rejection', rejected: true, result: { code: 'delete_failed', message: 'delete failed' } },
  ])('keeps an in-flight selection read valid after DELETE $label', async ({ rejected, result }) => {
    const agent = brief('agent-a', 'A');
    const reconnect = deferred<ReturnType<typeof fullAgent>>();
    const removeVibeAgent = rejected ? vi.fn().mockRejectedValue(result) : vi.fn().mockResolvedValue(result);
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'initial'))
      .mockReturnValueOnce(reconnect.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, undefined, removeVibeAgent);
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: 'common.delete' }));
    await waitFor(() => expect(removeVibeAgent).toHaveBeenCalledWith('agent-a'));
    act(() => reconnect.resolve(fullAgent({ ...agent, description: 'A remains' }, 'fresh A')));
    await waitFor(() => expect(screen.getByDisplayValue('A remains')).toBeTruthy());
    expect(screen.getByText(result.message)).toBeTruthy();
    expect(getVibeAgent).toHaveBeenCalledTimes(2);
  });

  it('does not restore or auto-select a deleted A when a pending B selection fails', async () => {
    const agentA = brief('agent-a', 'A');
    const agentB = brief('agent-b', 'B');
    const pendingB = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(pendingB.promise)
      .mockResolvedValue(fullAgent(agentB, 'B retry'));
    const removeVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(
      vi.fn().mockResolvedValue(listResult([agentA, agentB])),
      getVibeAgent,
      undefined,
      undefined,
      undefined,
      removeVibeAgent,
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: 'common.delete' }));
    await waitFor(() => expect(removeVibeAgent).toHaveBeenCalledWith('agent-a'));
    act(() => pendingB.reject({ code: 'agent_backend_unavailable', message: 'B unavailable' }));
    await waitFor(() => expect(screen.queryByDisplayValue('A initial')).toBeNull());
    expect(getVibeAgent.mock.calls.slice(2).some(([name]) => name === 'agent-a')).toBe(false);
  });

  it.each(['agent_not_found', 'agent_access_forbidden'] as const)(
    'retires an expected disappearance without recursively refreshing Definitions (%s)',
    async (code) => {
      const agentA = brief('agent-a', 'A');
      const getVibeAgent = vi.fn()
        .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
        .mockRejectedValueOnce({ code, message: 'gone' });
      const listVibeAgents = vi.fn().mockResolvedValue(listResult(agentA));
      const api = makeApi(listVibeAgents, getVibeAgent);
      renderPage(api);

      await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
      act(() => handlers?.onConnected?.());
      await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      // Detail retirement schedules one bounded Definitions follow-up so a
      // replacement identity can be discovered; it must not recurse further.
      expect(listVibeAgents).toHaveBeenCalledTimes(3);
      expect(screen.queryByDisplayValue('A')).toBeNull();
      expect(screen.queryByText('agent-a')).toBeNull();
      expect(showToast).not.toHaveBeenCalled();
    },
  );

  it.each([
    { label: 'thrown failure', reject: true },
    { label: 'HTTP ok false', reject: false },
  ])('keeps a current mutation $label visible through its drain and Definitions refresh', async ({ reject }) => {
    const agentA = brief('agent-a', 'A');
    const drain = deferred<ReturnType<typeof fullAgent>>();
    const patch = deferred<unknown>();
    const getVibeAgent = vi.fn().mockResolvedValueOnce(fullAgent(agentA, 'A initial')).mockReturnValueOnce(drain.promise);
    const updateVibeAgent = vi.fn().mockReturnValue(patch.promise);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agentA)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('A'), { target: { value: 'A local' } });
    fireEvent.blur(screen.getByDisplayValue('A local'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledTimes(1));
    if (reject) {
      act(() => patch.reject({ message: 'A mutation failed' }));
    } else {
      act(() => patch.resolve({ ok: false, message: 'A mutation failed' }));
    }
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => drain.resolve(fullAgent({ ...agentA, description: 'A server' }, 'A server prompt')));
    await waitFor(() => expect(screen.getByText('A mutation failed')).toBeTruthy());
    expect(screen.getByText('A mutation failed')).toBeTruthy();
  });

  it.each([
    {
      label: 'an ok-false response',
      response: { ok: false, message: 'server detail rejected' },
      expected: 'server detail rejected',
    },
    {
      label: 'a mismatched response',
      response: fullAgent(brief('agent-b', 'B'), 'wrong identity'),
      expected: 'errorBoundary.title',
    },
  ])('rolls back a selected load for $label with a localized fallback', async ({ response, expected }) => {
    const agentA = brief('agent-a', 'A');
    const getVibeAgent = vi.fn().mockResolvedValueOnce(fullAgent(agentA, 'A initial')).mockResolvedValueOnce(response);
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agentA)), getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByText(expected)).toBeTruthy());
    expect(screen.getByDisplayValue('A')).toBeTruthy();
  });

  it('silences expected selected disappearance but surfaces unexpected current failures', async () => {
    const agent = brief('agent-a', 'agent description');
    const expectedApi = makeApi(
      vi.fn().mockResolvedValue(listResult(agent)),
      vi.fn()
        .mockResolvedValueOnce(fullAgent(agent, 'prompt'))
        .mockRejectedValueOnce({ code: 'agent_not_found', message: 'gone' }),
    );
    renderPage(expectedApi);
    await waitFor(() => expect(screen.getByDisplayValue('agent description')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.queryByDisplayValue('agent description')).toBeNull());
    expect(showToast).not.toHaveBeenCalled();

    cleanup();
    handlers = null;
    const unexpectedApi = makeApi(
      vi.fn().mockResolvedValue(listResult(agent)),
      vi.fn()
        .mockResolvedValueOnce(fullAgent(agent, 'prompt'))
        .mockRejectedValueOnce({ code: 'agent_backend_unavailable', message: 'backend unavailable' }),
    );
    renderPage(unexpectedApi);
    await waitFor(() => expect(screen.getByDisplayValue('agent description')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByText('backend unavailable')).toBeTruthy());
  });

  it('reconciles organization onboarding inventory only from the gap owner', async () => {
    const agent = brief('agent-a', 'agent');
    const onboarding = (notOnboarded: number) => ({
      ok: true,
      available: true,
      organization_id: 'org-1',
      agents: [],
      counts: { total: 1, system: 0, custom: 1, not_onboarded: notOnboarded, private: 1 - notOnboarded, published: 0, conflicts: 0 },
    });
    const getVibeAgentOnboarding = vi.fn()
      .mockResolvedValueOnce(onboarding(1))
      .mockResolvedValueOnce(onboarding(0));
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), vi.fn().mockResolvedValue({ ok: false }), getVibeAgentOnboarding);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByText('agents.onboarding.notOnboardedCount:{"count":1}')).toBeTruthy());
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByText('agents.onboarding.notOnboardedCount:{"count":0}')).toBeTruthy());

    expect(getVibeAgentOnboarding).toHaveBeenCalledTimes(2);
    act(() => {
      handlers?.onEventBridgeStatus?.({ connected: false });
      handlers?.onEventBridgeStatus?.({ connected: true });
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(getVibeAgentOnboarding).toHaveBeenCalledTimes(2);
  });

  it.each(['agent_not_found', 'agent_access_forbidden'] as const)(
    'restores accepted A when pending B disappears (%s)',
    async (code) => {
      const agentA = brief('agent-a', 'A');
      const agentB = brief('agent-b', 'B');
      const pendingB = deferred<ReturnType<typeof fullAgent>>();
      const getVibeAgent = vi.fn()
        .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
        .mockReturnValueOnce(pendingB.promise)
        .mockResolvedValueOnce(fullAgent({ ...agentA, description: 'A after gap' }, 'A refreshed'))
        .mockResolvedValueOnce(fullAgent({ ...agentA, description: 'A after mutation' }, 'A mutation drain'));
      const updateVibeAgent = vi.fn().mockResolvedValue(fullAgent(agentA, 'ack'));
      const api = makeApi(
        vi.fn().mockResolvedValue(listResult([agentA, agentB])),
        getVibeAgent,
        undefined,
        undefined,
        updateVibeAgent,
      );
      renderPage(api, { canManageAgents: true });

      await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
      fireEvent.click(screen.getByText('agent-b').closest('button')!);
      await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
      act(() => pendingB.reject({ code, message: 'B disappeared' }));
      await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());

      act(() => handlers?.onConnected?.());
      await waitFor(() => expect(screen.getByDisplayValue('A after gap')).toBeTruthy());
      expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-a', {
        cache: false,
        expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
      });
      expect(getVibeAgent.mock.calls.slice(2).some(([name]) => name === 'agent-b')).toBe(false);

      fireEvent.change(screen.getByDisplayValue('A after gap'), { target: { value: 'A local' } });
      fireEvent.blur(screen.getByDisplayValue('A local'));
      await waitFor(() => expect(screen.getByDisplayValue('A after mutation')).toBeTruthy());
      expect(getVibeAgent).toHaveBeenNthCalledWith(4, 'agent-a', {
        cache: false,
        expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
      });
    },
  );

  it('migrates a stable-id rename and keeps the reconciled name clean', async () => {
    const oldAgent = brief('agent-old', 'description');
    const renamedAgent = { ...oldAgent, name: 'agent-new', display_name: 'agent-new' };
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(oldAgent, 'prompt'))
      .mockResolvedValueOnce(fullAgent(renamedAgent, 'prompt'));
    const updateVibeAgent = vi.fn().mockResolvedValue(fullAgent(renamedAgent, 'prompt'));
    const listVibeAgents = vi.fn()
      .mockResolvedValueOnce(listResult(oldAgent))
      .mockResolvedValue(listResult(renamedAgent));
    const api = makeApi(listVibeAgents, getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('agent-old')).toBeTruthy());
    const nameInput = screen.getByDisplayValue('agent-old');
    fireEvent.change(nameInput, { target: { value: 'agent-new' } });
    fireEvent.blur(screen.getByDisplayValue('agent-new'));
    await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-old', { name: 'agent-new' }));
    await waitFor(() => expect(screen.getByDisplayValue('agent-new')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenNthCalledWith(2, 'agent-new', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });

    fireEvent.blur(screen.getByDisplayValue('agent-new'));
    expect(updateVibeAgent).toHaveBeenCalledTimes(1);

    updateVibeAgent.mockRejectedValueOnce({ message: 'rename failed' });
    fireEvent.change(screen.getByDisplayValue('agent-new'), { target: { value: 'agent-lost' } });
    fireEvent.blur(screen.getByDisplayValue('agent-lost'));
    await waitFor(() => expect(screen.getByDisplayValue('agent-new')).toBeTruthy());
  });

  it.each(['agent_not_found', 'agent_access_forbidden'] as const)(
    'localizes rename retirement errors without manufacturing English (%s)',
    async (code) => {
      const oldAgent = brief('agent-old', 'description');
      const renamedAgent = { ...oldAgent, name: 'agent-new', display_name: 'agent-new' };
      const getVibeAgent = vi.fn()
        .mockResolvedValueOnce(fullAgent(oldAgent, 'prompt'))
        .mockRejectedValueOnce({ code });
      const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
      const listVibeAgents = vi.fn()
        .mockResolvedValueOnce(listResult(oldAgent))
        .mockResolvedValue(listResult(renamedAgent));
      const api = makeApi(listVibeAgents, getVibeAgent, undefined, undefined, updateVibeAgent);
      renderPage(api, { canManageAgents: true });

      await waitFor(() => expect(screen.getByDisplayValue('agent-old')).toBeTruthy());
      fireEvent.change(screen.getByDisplayValue('agent-old'), { target: { value: 'agent-new' } });
      fireEvent.blur(screen.getByDisplayValue('agent-new'));
      await waitFor(() => expect(updateVibeAgent).toHaveBeenCalledWith('agent-old', { name: 'agent-new' }));
      await waitFor(() => expect(screen.getByText('errorBoundary.title')).toBeTruthy());
      expect(screen.queryByText('Agent is no longer available')).toBeNull();
    },
  );

  it('lets a newer new-name read satisfy a superseded rename drain', async () => {
    const oldAgent = brief('agent-old', 'description');
    const renamedAgent = { ...oldAgent, name: 'agent-new', display_name: 'agent-new' };
    const oldDrain = deferred<ReturnType<typeof fullAgent>>();
    const newRead = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(oldAgent, 'prompt'))
      .mockReturnValueOnce(oldDrain.promise)
      .mockReturnValueOnce(newRead.promise);
    const updateVibeAgent = vi.fn().mockResolvedValue({ ok: true });
    const api = makeApi(
      vi.fn()
        .mockResolvedValueOnce(listResult(oldAgent))
        .mockResolvedValue(listResult(renamedAgent)),
      getVibeAgent,
      undefined,
      undefined,
      updateVibeAgent,
    );
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('agent-old')).toBeTruthy());
    fireEvent.change(screen.getByDisplayValue('agent-old'), { target: { value: 'agent-new' } });
    fireEvent.blur(screen.getByDisplayValue('agent-new'));
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));
    act(() => newRead.resolve(fullAgent(renamedAgent, 'prompt')));
    await waitFor(() => expect(screen.getByDisplayValue('agent-new')).toBeTruthy());
    act(() => oldDrain.resolve(fullAgent(renamedAgent, 'stale rename drain')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    fireEvent.blur(screen.getByDisplayValue('agent-new'));
    expect(updateVibeAgent).toHaveBeenCalledTimes(1);
  });

  it('cancels modal-only prompt edits without dirtying the shared field', async () => {
    const agent = brief('agent-a', 'description');
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agent, 'server prompt'))
      .mockResolvedValueOnce(fullAgent(agent, 'new server prompt'))
      .mockResolvedValueOnce(fullAgent(agent, 'latest server prompt'));
    const updateVibeAgent = vi.fn();
    const api = makeApi(vi.fn().mockResolvedValue(listResult(agent)), getVibeAgent, undefined, undefined, updateVibeAgent);
    renderPage(api, { canManageAgents: true });

    await waitFor(() => expect(screen.getByDisplayValue('description')).toBeTruthy());
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    const modal = screen.getByRole('dialog');
    fireEvent.change(screen.getByPlaceholderText('agents.create.systemPromptPlaceholder'), {
      target: { value: 'canceled modal text' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'common.cancel' }));
    expect(updateVibeAgent).not.toHaveBeenCalled();

    act(() => handlers?.onConnected?.());
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    await waitFor(() => expect(screen.getByDisplayValue('new server prompt')).toBeTruthy());
    fireEvent.change(screen.getByPlaceholderText('agents.create.systemPromptPlaceholder'), {
      target: { value: 'x-canceled modal text' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(updateVibeAgent).not.toHaveBeenCalled();

    act(() => handlers?.onConnected?.());
    fireEvent.click(screen.getByRole('button', { name: 'agents.detail.systemPromptExpand' }));
    await waitFor(() => expect(screen.getByDisplayValue('latest server prompt')).toBeTruthy());
    expect(modal).not.toBeNull();
  });

  it.each([
    { label: 'rejected expected disappearance', reject: true },
    { label: 'ok-false expected disappearance', reject: false },
  ])('continues the same catch-up edge to accepted A after B $label', async ({ reject }) => {
    const agentA = brief('agent-a', 'A before gap');
    const agentB = brief('agent-b', 'B');
    const preGapB = deferred<ReturnType<typeof fullAgent>>();
    const catchupA = fullAgent({ ...agentA, description: 'A changed during gap' }, 'A changed prompt');
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(preGapB.promise)
      .mockImplementationOnce(() =>
        reject
          ? Promise.reject({ code: 'agent_not_found', message: 'B disappeared' })
          : Promise.resolve({ ok: false, code: 'agent_not_found', message: 'B disappeared' }),
      )
      .mockResolvedValueOnce(catchupA);
    const api = makeApi(vi.fn().mockResolvedValue(listResult([agentA, agentB])), getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A before gap')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue('A changed during gap')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-b', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
    expect(getVibeAgent).toHaveBeenNthCalledWith(4, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });

    act(() => preGapB.resolve(fullAgent(agentB, 'stale B')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByDisplayValue('A changed during gap')).toBeTruthy();
    expect(getVibeAgent.mock.calls.slice(3).some(([name]) => name === 'agent-b')).toBe(false);
  });

  it('does not retry a failed resumed A until the next reconnect edge', async () => {
    const agentA = brief('agent-a', 'A before gap');
    const agentB = brief('agent-b', 'B');
    const preGapB = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(preGapB.promise)
      .mockRejectedValueOnce({ code: 'agent_not_found', message: 'B disappeared' })
      .mockRejectedValueOnce({ code: 'agent_backend_unavailable', message: 'A unavailable' })
      .mockResolvedValueOnce(fullAgent({ ...agentA, description: 'A after next gap' }, 'A recovered'));
    const api = makeApi(vi.fn().mockResolvedValue(listResult([agentA, agentB])), getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A before gap')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(4));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getVibeAgent).toHaveBeenCalledTimes(4);

    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue('A after next gap')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenNthCalledWith(5, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
  });

  it('retains auto-selection after a transient detail failure until the next edge', async () => {
    const agent = brief('agent-a', 'A');
    const getVibeAgent = vi.fn()
      .mockRejectedValueOnce({ message: 'temporary detail failure' })
      .mockResolvedValueOnce(fullAgent({ ...agent, description: 'A recovered' }, 'prompt'));
    const api = makeApi(
      vi.fn().mockResolvedValue(listResult(agent)),
      getVibeAgent,
    );
    renderPage(api);

    await waitFor(() => expect(screen.getByText('temporary detail failure')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenCalledTimes(1);
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(screen.getByDisplayValue('A recovered')).toBeTruthy());
    expect(getVibeAgent).toHaveBeenCalledTimes(2);
    expect(getVibeAgent).toHaveBeenNthCalledWith(2, 'agent-a', {
      cache: false,
      expectedCodes: ['agent_not_found', 'agent_access_forbidden'],
    });
  });

  it('does not resume A when a newer C intent wins during the B catch-up read', async () => {
    const agentA = brief('agent-a', 'A');
    const agentB = brief('agent-b', 'B');
    const agentC = brief('agent-c', 'C');
    const preGapB = deferred<ReturnType<typeof fullAgent>>();
    const catchupB = deferred<ReturnType<typeof fullAgent>>();
    const getVibeAgent = vi.fn()
      .mockResolvedValueOnce(fullAgent(agentA, 'A initial'))
      .mockReturnValueOnce(preGapB.promise)
      .mockReturnValueOnce(catchupB.promise)
      .mockResolvedValueOnce(fullAgent(agentC, 'C current'));
    const api = makeApi(vi.fn().mockResolvedValue(listResult([agentA, agentB, agentC])), getVibeAgent);
    renderPage(api);

    await waitFor(() => expect(screen.getByDisplayValue('A')).toBeTruthy());
    fireEvent.click(screen.getByText('agent-b').closest('button')!);
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(2));
    act(() => handlers?.onConnected?.());
    await waitFor(() => expect(getVibeAgent).toHaveBeenCalledTimes(3));

    fireEvent.click(screen.getByText('agent-c').closest('button')!);
    await waitFor(() => expect(screen.getByDisplayValue('C')).toBeTruthy());
    act(() => catchupB.reject({ code: 'agent_not_found', message: 'B disappeared' }));
    act(() => preGapB.resolve(fullAgent(agentB, 'stale B')));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByDisplayValue('C')).toBeTruthy();
    expect(getVibeAgent.mock.calls.slice(3).some(([name]) => name === 'agent-a')).toBe(false);
  });
});
