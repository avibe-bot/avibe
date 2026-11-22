/** @vitest-environment jsdom */

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { RunningAgent } from '../../context/ApiContext';
import { AgentGraphTab } from './AgentGraphTab';

const api = {
  getAgentsGraph: vi.fn(),
  getRunningAgents: vi.fn(),
  listProjects: vi.fn(),
  connectWorkbenchEvents: vi.fn(),
  endRunningAgent: vi.fn(),
};

vi.mock('../../context/ApiContext', () => ({ useApi: () => api }));
vi.mock('../../context/ToastContext', () => ({ useToast: () => ({ showToast: vi.fn() }) }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
  }),
}));

vi.stubGlobal('ResizeObserver', class {
  observe() {}
  unobserve() {}
  disconnect() {}
});

// A session-less live runtime: the running-agents snapshot is the only source
// of these rows, shown in the strip above the graph.
const sessionless: RunningAgent = {
  backend: 'codex',
  state: 'orphan',
  base_session_id: null,
  composite_key: 'codex:orphan-1',
  workdir: '/tmp/work',
  pid: 4242,
  pid_shared: false,
  native_session_id: null,
  model: null,
  elapsed_seconds: 60,
  session_id: null,
  title: null,
  platform: null,
  scope_type: null,
  scope_display_name: null,
  visibility: null,
  trigger_source: null,
  agent_name: null,
  openable_in_chat: false,
};

function renderTab() {
  return render(
    <MemoryRouter>
      <AgentGraphTab />
    </MemoryRouter>,
  );
}

const stripTitle = 'agents.graph.orphans.title:{"count":1}';

describe('AgentGraphTab running list vs. graph loading', () => {
  beforeEach(() => {
    api.getRunningAgents.mockResolvedValue({ ok: true, agents: [sessionless] });
    api.listProjects.mockResolvedValue({ projects: [] });
    api.connectWorkbenchEvents.mockReturnValue(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('shows the running list while the graph request is still pending', async () => {
    api.getAgentsGraph.mockReturnValue(new Promise(() => {}));

    renderTab();

    expect(await screen.findByText(stripTitle)).toBeTruthy();
    // The graph area keeps its ordinary loading state meanwhile.
    expect(screen.queryByText('agents.graph.error')).toBeNull();
    expect(screen.queryByText('agents.graph.empty')).toBeNull();
  });

  it('keeps the running list when the graph request fails', async () => {
    api.getAgentsGraph.mockRejectedValue(new Error('graph failed'));

    renderTab();

    expect(await screen.findByText('agents.graph.error')).toBeTruthy();
    expect(screen.getByText(stripTitle)).toBeTruthy();
  });
});
