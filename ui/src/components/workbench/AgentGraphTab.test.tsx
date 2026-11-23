/** @vitest-environment jsdom */

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key: string) => key }) }));

vi.stubGlobal('ResizeObserver', class {
  observe() {}
  unobserve() {}
  disconnect() {}
});

function renderTab() {
  return render(
    <MemoryRouter>
      <AgentGraphTab />
    </MemoryRouter>,
  );
}

const modeRadio = (key: 'active' | 'withHistory') =>
  screen.getByRole('radio', { name: `agents.graph.filters.${key}` });

describe('AgentGraphTab 活跃/含历史 mode', () => {
  beforeEach(() => {
    api.getAgentsGraph.mockResolvedValue({ ok: true, nodes: [], edges: [], triggers: [] });
    api.getRunningAgents.mockResolvedValue({ ok: true, agents: [] });
    api.listProjects.mockResolvedValue({ projects: [] });
    api.connectWorkbenchEvents.mockReturnValue(() => {});
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    window.localStorage.clear();
  });

  it('opens on 活跃 every visit, even after choosing 含历史 or with an older stored choice', async () => {
    // What earlier builds stored when the user left the tab on 含历史.
    window.localStorage.setItem('avibe.agents.graph-mode.v1', 'history');

    const first = renderTab();
    expect(modeRadio('active').getAttribute('aria-checked')).toBe('true');
    await waitFor(() =>
      expect(api.getAgentsGraph).toHaveBeenLastCalledWith(expect.objectContaining({ includeEnded: false })),
    );

    fireEvent.click(modeRadio('withHistory'));
    expect(modeRadio('withHistory').getAttribute('aria-checked')).toBe('true');
    await waitFor(() =>
      expect(api.getAgentsGraph).toHaveBeenLastCalledWith(expect.objectContaining({ includeEnded: true })),
    );

    // Leave the tab and come back.
    first.unmount();
    api.getAgentsGraph.mockClear();
    renderTab();

    expect(modeRadio('active').getAttribute('aria-checked')).toBe('true');
    await waitFor(() => expect(api.getAgentsGraph).toHaveBeenCalled());
    expect(api.getAgentsGraph).toHaveBeenLastCalledWith(expect.objectContaining({ includeEnded: false }));
  });

  it('opens with disabled triggers hidden every visit, even after showing them or with an older stored choice', () => {
    // What earlier builds stored when the user turned 显示已禁用 on.
    window.localStorage.setItem('vibe-remote:graph-show-disabled', '1');
    const showDisabled = () => screen.getByRole('switch', { name: 'agents.graph.legend.showDisabled' });

    const first = renderTab();
    expect(showDisabled().getAttribute('aria-checked')).toBe('false');
    fireEvent.click(showDisabled());
    expect(showDisabled().getAttribute('aria-checked')).toBe('true');
    // The choice is not stored.
    expect(window.localStorage.getItem('vibe-remote:graph-show-disabled')).toBe('1');

    // Leave the tab and come back.
    first.unmount();
    renderTab();
    expect(showDisabled().getAttribute('aria-checked')).toBe('false');
  });
});
