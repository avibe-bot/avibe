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
  getRunningAgents: ReturnType<typeof vi.fn>;
  connectWorkbenchEvents: ReturnType<typeof vi.fn>;
};

const apiRef = vi.hoisted(() => ({ current: null as FakeApi | null }));
let handlers: WorkbenchEventHandlers | null = null;

vi.mock('../../context/ApiContext', async () => {
  const actual = await vi.importActual<typeof import('../../context/ApiContext')>('../../context/ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});

vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
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

const listResult = (agent: VibeAgentBrief) => ({
  ok: true,
  agents: [agent],
  default_agent_name: agent.name,
});

function makeApi(listVibeAgents: FakeApi['listVibeAgents']): FakeApi {
  return {
    listVibeAgents,
    getVibeAgent: vi.fn().mockResolvedValue({ ok: false }),
    getVibeAgentOnboarding: vi.fn().mockResolvedValue({ available: false }),
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
    capabilities = { ...OWNER_INSTANCE_CAPABILITIES, can_manage_agents: false },
  }: {
    remote?: boolean;
    instanceKind?: 'organization' | 'personal' | null;
    instanceRole?: InstanceRole;
    capabilities?: InstanceCapabilities;
  } = {},
) {
  apiRef.current = api;
  return render(
    <InstanceAuthorizationContext.Provider value={{ remote, instanceKind, instanceRole, capabilities }}>
      <MemoryRouter initialEntries={['/agents']}>
        <AgentsPage />
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

afterEach(() => {
  cleanup();
  apiRef.current = null;
  handlers = null;
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

    expect(listVibeAgents).toHaveBeenNthCalledWith(1, { includeDisabled: true });
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
});
