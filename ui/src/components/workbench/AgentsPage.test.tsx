/* @vitest-environment jsdom */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import type { InstanceCapabilities, InstanceRole } from '../../lib/sessionInfo';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { AgentsPage } from './AgentsPage';

const api = vi.hoisted(() => ({
  listVibeAgents: vi.fn(),
  getVibeAgent: vi.fn(),
  getVibeAgentOnboarding: vi.fn(),
  getRunningAgents: vi.fn(),
  connectWorkbenchEvents: vi.fn(),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock('../../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/ApiContext')>()),
  useApi: () => api,
}));

vi.mock('../../context/ToastContext', () => ({
  useToast: () => ({ showToast: vi.fn() }),
}));

// The detail panel's model catalog is an HTTP read of its own; serve it locally
// so this file is about which requests the page load issues, not about the
// catalog's contents.
vi.mock('../../lib/backendModels', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/backendModels')>()),
  loadBackendModelsWithRefresh: (
    _api: unknown,
    _backend: string,
    onLoaded: (payload: { models: string[] }) => void,
  ) => {
    onLoaded({ models: [] });
    return () => {};
  },
}));

vi.mock('./AgentGraphTab', () => ({ AgentGraphTab: () => null }));
vi.mock('./NewAgentDialog', () => ({ NewAgentDialog: () => null }));
vi.mock('./RunAgentDialog', () => ({ RunAgentDialog: () => null }));
vi.mock('./GlobalPromptsDialog', () => ({ GlobalPromptsDialog: () => null }));

const AGENT = {
  id: 'agt-claude',
  name: 'claude',
  display_name: 'claude',
  description: null,
  backend: 'claude',
  model: 'sonnet',
  reasoning_effort: null,
  enabled: true,
  archived: false,
  archived_at: null,
  source: 'builtin',
  updated_at: '2026-08-19T00:00:00Z',
};

// A remote Instance Member: the rank keeps Agent CRUD (`can_manage_agents`) but
// is not the Instance Owner. Bulk onboarding is Owner-only on the HTTP policy,
// so this is exactly the projection that used to 403 on page load.
const MEMBER_CAPABILITIES: InstanceCapabilities = {
  ...OWNER_INSTANCE_CAPABILITIES,
  is_instance_owner: false,
  can_manage_instance: false,
  can_manage_access_members: false,
};

function renderPage(instanceRole: InstanceRole, capabilities: InstanceCapabilities) {
  return render(
    <InstanceAuthorizationContext.Provider
      value={{ remote: true, instanceKind: 'organization', instanceRole, capabilities }}
    >
      <MemoryRouter initialEntries={['/agents']}>
        <AgentsPage />
      </MemoryRouter>
    </InstanceAuthorizationContext.Provider>,
  );
}

// jsdom has no layout, so the capability tab strip's scroll-into-view is a
// no-op here; the page itself stays real.
const originalScrollIntoView = Element.prototype.scrollIntoView;

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  api.listVibeAgents.mockReset();
  api.listVibeAgents.mockResolvedValue({ ok: true, agents: [AGENT], default_agent_name: 'claude' });
  api.getVibeAgent.mockReset();
  api.getVibeAgent.mockResolvedValue({ ok: true, agent: AGENT });
  api.getVibeAgentOnboarding.mockReset();
  api.getVibeAgentOnboarding.mockResolvedValue({ available: false });
  api.getRunningAgents.mockReset();
  api.getRunningAgents.mockResolvedValue({ ok: true, counts: { total: 0 } });
  api.connectWorkbenchEvents.mockReset();
  api.connectWorkbenchEvents.mockReturnValue(() => {});
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  Element.prototype.scrollIntoView = originalScrollIntoView;
});

describe('AgentsPage load requests follow the rank that can serve them', () => {
  it('does not request the Owner-only onboarding inventory for a member', async () => {
    const view = renderPage('member', MEMBER_CAPABILITIES);

    // Wait on a request the member IS entitled to, so the assertion below runs
    // after the page load has actually settled rather than before it starts.
    await waitFor(() => expect(api.listVibeAgents).toHaveBeenCalled());
    await waitFor(() => expect(api.getVibeAgent).toHaveBeenCalled());
    expect(api.getVibeAgentOnboarding).not.toHaveBeenCalled();
    view.unmount();
  });

  it('still requests the onboarding inventory for the instance owner', async () => {
    const view = renderPage('owner', OWNER_INSTANCE_CAPABILITIES);

    await waitFor(() => expect(api.getVibeAgentOnboarding).toHaveBeenCalled());
    view.unmount();
  });
});
