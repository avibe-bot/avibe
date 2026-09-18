/* @vitest-environment jsdom */

import { createInstance } from 'i18next';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { I18nextProvider, initReactI18next } from 'react-i18next';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../i18n/en.json';
import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import type { InstanceRole } from '../../lib/sessionInfo';
import { capabilitiesFor } from '../../lib/testing/instanceRoleCapabilities';
import { HarnessPage } from './HarnessPage';

// PERMISSIONS-022 — the global background-work banner is one shared instance
// preference behind the member-tier `PUT /api/workbench/prefs`, while the
// Harness page itself is an editor surface. The switch has to read as read-only
// below member instead of optimistically flipping and silently reverting.

type FakeApi = {
  getWorkbenchPrefs: ReturnType<typeof vi.fn>;
  setBackgroundWorkBannerEnabled: ReturnType<typeof vi.fn>;
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

const emptyCounts = { tasks: {}, watches: {}, runs: {} };

const makeApi = (bannerEnabled = true): FakeApi => ({
  getWorkbenchPrefs: vi.fn().mockResolvedValue({ background_work_banner_enabled: bannerEnabled }),
  setBackgroundWorkBannerEnabled: vi
    .fn()
    .mockImplementation(async (next: boolean) => ({ background_work_banner_enabled: next })),
  getHarnessBootstrap: vi.fn().mockResolvedValue({
    counts: emptyCounts,
    page: { tasks: [], counts: {}, has_more: false },
  }),
  listVibeAgents: vi.fn().mockResolvedValue({ ok: true, agents: [], default_agent_name: null }),
  connectWorkbenchEvents: vi.fn().mockReturnValue(() => {}),
});

const renderAsRole = (role: InstanceRole, api: FakeApi) => {
  apiRef.current = api;
  return render(
    <I18nextProvider i18n={i18n}>
      <InstanceAuthorizationContext.Provider
        value={{
          remote: true,
          instanceKind: 'organization',
          instanceRole: role,
          capabilities: capabilitiesFor(role),
        }}
      >
        <MemoryRouter initialEntries={['/harness']}>
          <HarnessPage />
        </MemoryRouter>
      </InstanceAuthorizationContext.Provider>
    </I18nextProvider>,
  );
};

const bannerSwitch = () =>
  screen.getByRole('switch', { name: en.harness.bannerToggle.title }) as HTMLButtonElement;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  apiRef.current = null;
});

describe('PERMISSIONS-022 HarnessPage background-work banner follows instance-preference authority', () => {
  it('leaves the switch read-only for an editor and attempts no write', async () => {
    const api = makeApi();
    renderAsRole('editor', api);

    // The page itself still loads: Harness is an editor surface.
    await waitFor(() => expect(api.getHarnessBootstrap).toHaveBeenCalled());
    await waitFor(() => expect(api.getWorkbenchPrefs).toHaveBeenCalled());

    const control = bannerSwitch();
    expect(control.disabled).toBe(true);
    expect(screen.getByText(en.harness.bannerToggle.readOnlyHint)).toBeTruthy();
    expect(screen.queryByText(en.harness.bannerToggle.description)).toBeNull();

    fireEvent.click(control);
    expect(api.setBackgroundWorkBannerEnabled).not.toHaveBeenCalled();
    // No optimistic flip either — the rendered state still mirrors the server.
    expect(control.getAttribute('aria-checked')).toBe('true');
  });

  it.each<InstanceRole>(['member', 'owner'])('lets a %s persist the shared preference', async (role) => {
    const api = makeApi();
    renderAsRole(role, api);

    await waitFor(() => expect(api.getWorkbenchPrefs).toHaveBeenCalled());
    const control = bannerSwitch();
    await waitFor(() => expect(control.getAttribute('aria-checked')).toBe('true'));
    expect(control.disabled).toBe(false);
    expect(screen.getByText(en.harness.bannerToggle.description)).toBeTruthy();

    fireEvent.click(control);
    await waitFor(() => expect(api.setBackgroundWorkBannerEnabled).toHaveBeenCalledWith(false));
    await waitFor(() => expect(control.getAttribute('aria-checked')).toBe('false'));
  });

  it('refuses the write in the same render a member is downgraded to editor', async () => {
    const api = makeApi();
    const view = renderAsRole('member', api);
    await waitFor(() => expect(api.getWorkbenchPrefs).toHaveBeenCalled());
    expect(bannerSwitch().disabled).toBe(false);

    view.rerender(
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

    const control = bannerSwitch();
    expect(control.disabled).toBe(true);
    fireEvent.click(control);
    expect(api.setBackgroundWorkBannerEnabled).not.toHaveBeenCalled();
  });
});
