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

vi.mock('../../context/ApiContext', async () => {
  const actual = await vi.importActual<typeof import('../../context/ApiContext')>('../../context/ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast }) }));
vi.mock('./CapabilityTabs', () => ({ CapabilityTabs: () => null }));
vi.mock('../../lib/backendModels', async () => ({
  loadBackendModelsWithRefresh: (_api: unknown, _backend: string, onLoaded: (payload: { models: string[] }) => void) => {
    onLoaded({ models: [] });
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
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-b', { cache: false });
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
    expect(getVibeAgent).toHaveBeenNthCalledWith(2, 'agent-b', { cache: false });
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
    expect(screen.getByDisplayValue('local patch')).toBeTruthy();
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
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-b', { cache: false });
    expect(getVibeAgent).toHaveBeenNthCalledWith(4, 'agent-a', { cache: false });
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
    expect(screen.getByDisplayValue('local A')).toBeTruthy();
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
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-a', { cache: false });
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
    expect(getVibeAgent).toHaveBeenNthCalledWith(3, 'agent-a', { cache: false });
  });

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
});
