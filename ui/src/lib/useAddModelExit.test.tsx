/* @vitest-environment jsdom */
//
// Who sees a model picker's "Add model" exit, and for how long. The answer is
// only as good as the read behind it: Settings, which the exit opens, is also
// where a backend is switched to Direct, so an answer from before a visit may
// no longer hold and must not be shown while the next read is in flight.
import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentSupply } from '../components/settings/models/types';
import { InstanceAuthorizationContext } from '../context/InstanceAuthorizationContext';
import { useAddModelExit } from './backendModels';
import { RouteSurfaceActiveContext } from './routeSurfaceActivity';
import { settingsOverlayOpenState, settingsOverlayOriginFromState } from './settingsOverlay';
import { capabilitiesFor } from './testing/instanceRoleCapabilities';
import type { InstanceRole } from './sessionInfo';

type PickerAgent = Pick<AgentSupply, 'backend' | 'mode' | 'catalog_models'> | null;

// Each read waits for its own answer, so a test decides when a read lands.
const reads: { backend: string; answer: (agent: PickerAgent) => void }[] = [];
const API = {
  readModelHubAgentCatalogForModelPicker: vi.fn(
    (backend: string) => new Promise<PickerAgent>((answer) => reads.push({ backend, answer })),
  ),
};
vi.mock('../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../context/ApiContext')>()),
  useApi: () => API,
}));

const hub = (backend: AgentSupply['backend']): PickerAgent => ({ backend, mode: 'hub', catalog_models: [] });
const direct = (backend: AgentSupply['backend']): PickerAgent => ({ backend, mode: 'direct' });

const land = async (agent: PickerAgent) => {
  await act(async () => reads.at(-1)!.answer(agent));
};

type Entry = string | { pathname: string; state: unknown };

const setup = (initial: { backend: string; enabled?: boolean; surfaceActive?: boolean; role?: InstanceRole; entry?: Entry }) => {
  let surfaceActive = initial.surfaceActive ?? true;
  let role: InstanceRole = initial.role ?? 'owner';
  const wrapper = ({ children }: { children: ReactNode }) => (
    <InstanceAuthorizationContext.Provider
      value={{ remote: false, instanceKind: null, instanceRole: role, capabilities: capabilitiesFor(role) }}
    >
      <RouteSurfaceActiveContext.Provider value={surfaceActive}>
        <MemoryRouter initialEntries={[initial.entry ?? '/agents?tab=definitions']}>{children}</MemoryRouter>
      </RouteSurfaceActiveContext.Provider>
    </InstanceAuthorizationContext.Provider>
  );
  const view = renderHook(
    ({ backend, enabled }: { backend: string; enabled?: boolean }) => ({
      exit: useAddModelExit(backend, enabled),
      location: useLocation(),
    }),
    { wrapper, initialProps: { backend: initial.backend, enabled: initial.enabled } },
  );
  return {
    ...view,
    cover: (covered: boolean) => {
      surfaceActive = !covered;
      view.rerender({ backend: initial.backend, enabled: initial.enabled });
    },
    as: (next: InstanceRole) => {
      role = next;
      view.rerender({ backend: initial.backend, enabled: initial.enabled });
    },
  };
};

afterEach(() => {
  reads.length = 0;
  API.readModelHubAgentCatalogForModelPicker.mockClear();
});

describe('useAddModelExit', () => {
  it("opens the Hub backend's own catalog once the read confirms the Hub supplies it", async () => {
    const { result } = setup({ backend: 'codex' });
    expect(result.current.exit).toBeNull();

    await land(hub('codex'));
    act(() => result.current.exit!());

    const { pathname, search, state } = result.current.location;
    expect(`${pathname}${search}`).toBe('/settings/models?manage=codex');
    // A detour to come back from: the page it left, and any draft on it, stays
    // mounted behind Settings, which a phone does not do for an ordinary visit.
    expect(settingsOverlayOriginFromState(state)?.location).toMatchObject({
      pathname: '/agents',
      search: '?tab=definitions',
    });
  });

  it('keeps the Settings visit it starts in coming back to the same place', async () => {
    // The IM channel settings page is a Settings route over a session. A
    // Settings route is never an origin, so the exit must carry the visit's.
    const session = { pathname: '/chat/ses_1', search: '', hash: '', state: null, key: 'k1' };
    const { result } = setup({
      backend: 'codex',
      entry: { pathname: '/settings/platforms/groups', state: settingsOverlayOpenState(session, null) },
    });
    await land(hub('codex'));
    act(() => result.current.exit!());

    expect(result.current.location.pathname).toBe('/settings/models');
    expect(settingsOverlayOriginFromState(result.current.location.state)?.location.pathname).toBe('/chat/ses_1');
  });

  it.each([
    ['a Direct backend', direct('codex')],
    ['a failed read, which the API reports as no record', null],
    ['a record describing another backend', hub('claude')],
  ])('shows no exit for %s', async (_case, agent) => {
    const { result } = setup({ backend: 'codex' });
    await land(agent);
    expect(result.current.exit).toBeNull();
  });

  it('does not ask for a viewer the owner-only Model Hub would send home', () => {
    const { result } = setup({ backend: 'codex', role: 'editor' });
    expect(API.readModelHubAgentCatalogForModelPicker).not.toHaveBeenCalled();
    expect(result.current.exit).toBeNull();
  });

  it('reads nothing until the picker enables it', () => {
    setup({ backend: 'codex', enabled: false });
    expect(API.readModelHubAgentCatalogForModelPicker).not.toHaveBeenCalled();
  });

  it('drops the answer while Settings covers the surface and shows none until a new read lands', async () => {
    const view = setup({ backend: 'codex' });
    await land(hub('codex'));

    view.cover(true);
    expect(view.result.current.exit).toBeNull();

    view.cover(false);
    // The earlier answer is not lent to the read now in flight.
    expect(API.readModelHubAgentCatalogForModelPicker).toHaveBeenCalledTimes(2);
    expect(view.result.current.exit).toBeNull();

    // The visit switched the backend to Direct.
    await land(direct('codex'));
    expect(view.result.current.exit).toBeNull();
  });

  it("does not lend one backend's answer to the next", async () => {
    const view = setup({ backend: 'codex' });
    await land(hub('codex'));

    view.rerender({ backend: 'claude', enabled: undefined });
    expect(view.result.current.exit).toBeNull();

    // A late answer for the backend it has left changes nothing.
    await act(async () => reads[0].answer(hub('codex')));
    expect(view.result.current.exit).toBeNull();
    await land(hub('claude'));
    expect(view.result.current.exit).not.toBeNull();
  });
});
