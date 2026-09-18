/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { capabilitiesFor } from '../../lib/testing/instanceRoleCapabilities';
import { HarnessPage } from './HarnessPage';

// PERMISSIONS-016 — `runs.updated` is admitted at the Editor tier that owns
// `/api/harness/*`, and below runtime management the controller reduces the
// frame to its type alone. The Harness page has no polling fallback, so the
// stream is the only thing that wakes it: this drives the real page from a bare
// frame rather than asserting on a stream stub that never emits.

type WorkbenchEventHandlers = { onRunsUpdated?: (data: unknown) => void };

const eventHandlers: WorkbenchEventHandlers[] = [];

type FakeApi = {
  getWorkbenchPrefs: ReturnType<typeof vi.fn>;
  getHarnessBootstrap: ReturnType<typeof vi.fn>;
  listVibeAgents: ReturnType<typeof vi.fn>;
  connectWorkbenchEvents: ReturnType<typeof vi.fn>;
};

const apiRef = vi.hoisted(() => ({ current: null as FakeApi | null }));

vi.mock('../../context/ApiContext', async () => {
  const actual = await vi.importActual<typeof import('../../context/ApiContext')>('../../context/ApiContext');
  return { ...actual, useApi: () => apiRef.current };
});
vi.mock('./CapabilityTabs', () => ({ CapabilityTabs: () => null }));

const i18n = createInstance();
void i18n.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
});

const makeApi = (): FakeApi => ({
  getWorkbenchPrefs: vi.fn().mockResolvedValue({ background_work_banner_enabled: false }),
  getHarnessBootstrap: vi.fn().mockResolvedValue({
    counts: { tasks: {}, watches: {}, runs: {} },
    page: { tasks: [], counts: {}, has_more: false },
  }),
  listVibeAgents: vi.fn().mockResolvedValue({ ok: true, agents: [], default_agent_name: null }),
  connectWorkbenchEvents: vi.fn((handlers: WorkbenchEventHandlers) => {
    eventHandlers.push(handlers);
    return () => {
      const at = eventHandlers.indexOf(handlers);
      if (at >= 0) eventHandlers.splice(at, 1);
    };
  }),
});

const renderAsEditor = (api: FakeApi) => {
  apiRef.current = api;
  return render(
    <I18nextProvider i18n={i18n}>
      <InstanceAuthorizationContext.Provider
        value={{
          remote: true,
          instanceKind: 'organization',
          instanceRole: 'editor',
          capabilities: capabilitiesFor('editor'),
        }}
      >
        <MemoryRouter initialEntries={['/harness']}>
          <HarnessPage />
        </MemoryRouter>
      </InstanceAuthorizationContext.Provider>
    </I18nextProvider>,
  );
};

afterEach(() => {
  cleanup();
  eventHandlers.length = 0;
  vi.restoreAllMocks();
  apiRef.current = null;
});

describe('PERMISSIONS-016 HarnessPage wakes an Editor on the bare runs.updated frame', () => {
  it('reloads the Harness view from the signal alone', async () => {
    const api = makeApi();
    renderAsEditor(api);

    await waitFor(() => expect(api.getHarnessBootstrap).toHaveBeenCalled());
    const before = api.getHarnessBootstrap.mock.calls.length;
    const agentsBefore = api.listVibeAgents.mock.calls.length;

    await act(async () => {
      for (const handler of [...eventHandlers]) handler.onRunsUpdated?.({});
    });

    await waitFor(() => expect(api.getHarnessBootstrap.mock.calls.length).toBeGreaterThan(before));
    // The Agent catalog the rows are labelled from refreshes on the same signal.
    await waitFor(() => expect(api.listVibeAgents.mock.calls.length).toBeGreaterThan(agentsBefore));
  });
});
