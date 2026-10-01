/* @vitest-environment jsdom */

import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';

import type { VibeAgentBrief } from '../../context/ApiContext';
import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { capabilitiesFor } from '../../lib/testing/instanceRoleCapabilities';
import type { InstanceRole } from '../../lib/sessionInfo';
import { AgentRoutePicker } from './AgentRoutePicker';
import type { AgentRoutePatch, AgentRouteValue } from './AgentRoutePicker';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

// One client for every render, as the real context provides: the picker's
// catalog read keys on it, so a fresh object per render would re-read on its own.
// The Hub read answers per test; `useAddModelPath` owns what it means.
let hubMode: 'hub' | 'direct' = 'direct';
const API = {
  readModelHubAgentCatalogForModelPicker: (backend: string) => Promise.resolve({ backend, mode: hubMode }),
};
vi.mock('../../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/ApiContext')>()),
  useApi: () => API,
}));

// What the catalog answers for this render. A Hub catalog states one entry per
// model, so a test can say "this model has no efforts" the same way the server
// does — and an empty default keeps the backend fallback in play.
let catalogReasoning: Record<string, { value: string; label: string }[]> = {};
// How many catalog reads were made, and whether the next ones wait to be answered.
let catalogReads = 0;
let deferReads = false;
const pendingReads: (() => void)[] = [];

// The model column is fetched per backend; serve it synchronously so the test is
// about the route state, not about the catalog request.
vi.mock('../../lib/backendModels', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/backendModels')>()),
  loadBackendModelsWithRefresh: (
    _api: unknown,
    _backend: string,
    onLoaded: (payload: {
      models: string[];
      modelLabels: Record<string, string>;
      reasoningOptions: Record<string, { value: string; label: string }[]>;
      catalogRefreshPending: boolean;
    }) => void,
  ) => {
    catalogReads += 1;
    const answer = () => onLoaded({
      models: ['sonnet', 'opus'],
      modelLabels: {},
      reasoningOptions: catalogReasoning,
      catalogRefreshPending: false,
    });
    if (deferReads) pendingReads.push(answer);
    else answer();
    return () => {};
  },
}));

const agent = (over: Partial<VibeAgentBrief> = {}): VibeAgentBrief =>
  ({
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
    ...over,
  }) as VibeAgentBrief;

const AGENTS = [agent(), agent({ id: 'agt-codex', name: 'codex', display_name: 'codex', backend: 'codex', model: 'gpt' })];

const CLAUDE_ROUTE: AgentRouteValue = {
  agent_name: 'claude',
  agent_id: 'agt-claude',
  agent_backend: 'claude',
  agent_variant: 'claude',
  model: 'sonnet',
  reasoning_effort: 'low',
};

// The picker holds no selection of its own, so its highlight is whatever the
// owner passes back as `value`. This stands in for an owner whose write is
// async: the pick lands in local state within the click and `saving` stays on
// until the test releases it — exactly what the two workbench owners now do.
const OptimisticOwner: React.FC<{
  initial?: AgentRouteValue;
  isDefaultRoute?: boolean;
  defaultRoute?: AgentRouteValue;
  defaultLabel?: string;
  onWrite: (patch: AgentRoutePatch) => void;
}> = ({ initial = CLAUDE_ROUTE, isDefaultRoute, defaultRoute, defaultLabel, onWrite }) => {
  const [route, setRoute] = useState<AgentRouteValue>(initial);
  const [saving, setSaving] = useState(false);
  return (
    <MemoryRouter>
      <AgentRoutePicker
        value={route}
        agents={AGENTS}
        saving={saving}
        isDefaultRoute={isDefaultRoute}
        defaultRoute={defaultRoute}
        defaultLabel={defaultLabel}
        onChange={(patch) => {
          setRoute((prev) => ({ ...prev, ...patch }));
          setSaving(true);
          onWrite(patch);
        }}
      />
    </MemoryRouter>
  );
};

// The highlight IS the checkmark in this menu: the active row carries the cyan
// wash from `RouteItem`.
const highlighted = (label: string) => {
  const item = screen.getByRole('button', { name: label });
  return item.className.includes('bg-cyan/[0.10]');
};

const openMenu = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(screen.getByRole('button', { name: /claude/ }));
  await screen.findByText('chat.picker.model');
};

const LocationProbe = () => {
  const location = useLocation();
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
};

// A picker on a surface Settings can cover, seen by a viewer holding `role`.
const HubPicker: React.FC<{
  role: InstanceRole;
  surfaceActive?: boolean;
  onNavigateAway?: () => void;
}> = ({ role, surfaceActive = true, onNavigateAway }) => (
  <InstanceAuthorizationContext.Provider
    value={{ remote: false, instanceKind: null, instanceRole: role, capabilities: capabilitiesFor(role) }}
  >
    <RouteSurfaceActiveContext.Provider value={surfaceActive}>
      <MemoryRouter>
        <AgentRoutePicker value={CLAUDE_ROUTE} agents={AGENTS} onChange={() => {}} onNavigateAway={onNavigateAway} />
        <LocationProbe />
      </MemoryRouter>
    </RouteSurfaceActiveContext.Provider>
  </InstanceAuthorizationContext.Provider>
);

describe('AgentRoutePicker', () => {
  afterEach(() => {
    cleanup();
    catalogReasoning = {};
    hubMode = 'direct';
    catalogReads = 0;
    deferReads = false;
    pendingReads.length = 0;
  });

  it("ends a Hub backend's model list with an exit to that backend's catalog in the Model Hub", async () => {
    hubMode = 'hub';
    const user = userEvent.setup();
    const onNavigateAway = vi.fn();
    render(<HubPicker role="owner" onNavigateAway={onNavigateAway} />);
    await openMenu(user);

    const addModel = await screen.findByRole('button', { name: 'chat.picker.addModel' });
    const lastModel = screen.getByRole('button', { name: 'opus' });
    expect(lastModel.compareDocumentPosition(addModel) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    await user.click(addModel);

    expect(screen.getByTestId('location').textContent).toBe('/settings/models?manage=claude');
    expect(screen.queryByText('chat.picker.model')).toBeNull();
    // Settings opens over the host, which suspends under it with its draft
    // instead of being closed the way leaving for /agents closes it.
    expect(onNavigateAway).not.toHaveBeenCalled();
  });

  it('reads the model list again once Settings stops covering the surface', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<HubPicker role="owner" />);
    await openMenu(user);
    await user.keyboard('{Escape}');
    await openMenu(user);
    await user.keyboard('{Escape}');
    // Reopening alone serves the list already read.
    expect(catalogReads).toBe(1);

    rerender(<HubPicker role="owner" surfaceActive={false} />);
    rerender(<HubPicker role="owner" />);
    await openMenu(user);

    // The Model Hub may have changed the list while it was covered.
    expect(catalogReads).toBe(2);
  });

  it('offers no row read before a Settings visit while the read after it is in flight', async () => {
    // The visit may have switched the backend to Direct, where a Hub-only row
    // is a route that cannot run.
    const user = userEvent.setup();
    const { rerender } = render(<HubPicker role="owner" />);
    await openMenu(user);
    expect(screen.getByRole('button', { name: 'opus' })).toBeTruthy();

    deferReads = true;
    rerender(<HubPicker role="owner" surfaceActive={false} />);
    rerender(<HubPicker role="owner" />);

    expect(screen.queryByRole('button', { name: 'opus' })).toBeNull();
    expect(screen.getByText('common.loading')).toBeTruthy();
    // Nor an effort: without the catalog the backend's fallback ladder could
    // offer one the current model rejects.
    expect(screen.queryByText('chat.picker.effort')).toBeNull();
    act(() => pendingReads.shift()!());
    expect(screen.getByRole('button', { name: 'opus' })).toBeTruthy();
    expect(screen.getByText('chat.picker.effort')).toBeTruthy();
  });

  it('reads the list again for a menu left open while Settings covered it', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<HubPicker role="owner" />);
    await openMenu(user);
    expect(catalogReads).toBe(1);

    // A Settings command opens over the surface without dismissing the menu.
    rerender(<HubPicker role="owner" surfaceActive={false} />);
    rerender(<HubPicker role="owner" />);

    expect(screen.getByText('chat.picker.model')).toBeTruthy();
    expect(catalogReads).toBe(2);
  });

  it('highlights the pick while the owner write is still in flight', async () => {
    const user = userEvent.setup();
    const onWrite = vi.fn();
    render(<OptimisticOwner onWrite={onWrite} />);
    await openMenu(user);
    expect(highlighted('sonnet')).toBe(true);

    await user.click(screen.getByRole('button', { name: 'opus' }));

    // The write has not resolved — nothing here ever resolves it — and the
    // highlight has already moved.
    expect(onWrite).toHaveBeenCalledExactlyOnceWith({ model: 'opus' });
    expect(highlighted('opus')).toBe(true);
    expect(highlighted('sonnet')).toBe(false);
  });

  it('selects declared Off without changing the model or leaking it to another model', async () => {
    catalogReasoning = {
      sonnet: [{ value: 'low', label: 'Low' }, { value: 'none', label: 'Off' }],
      opus: [{ value: 'low', label: 'Low' }],
    };
    const user = userEvent.setup();
    const onWrite = vi.fn();
    render(<OptimisticOwner onWrite={onWrite} />);
    await openMenu(user);

    await user.click(screen.getByRole('button', { name: 'chat.picker.effortOptions.none' }));
    expect(onWrite).toHaveBeenCalledExactlyOnceWith({ reasoning_effort: 'none' });
    expect(highlighted('chat.picker.effortOptions.none')).toBe(true);

    await user.click(screen.getByRole('button', { name: 'opus' }));
    expect(onWrite).toHaveBeenLastCalledWith({ model: 'opus', reasoning_effort: null });
    expect(screen.queryByRole('button', { name: 'chat.picker.effortOptions.none' })).toBeNull();
  });

  it('keeps the menu clickable while a write is in flight, so the next pick is not lost', async () => {
    const user = userEvent.setup();
    const onWrite = vi.fn();
    render(<OptimisticOwner onWrite={onWrite} />);
    await openMenu(user);

    await user.click(screen.getByRole('button', { name: 'opus' }));
    const effort = screen.getByRole('button', { name: 'chat.picker.effortOptions.high' });
    // A disabled row would fire no click at all, which is how the previous
    // greyed-out panel dropped the effort a user picked right after a model.
    expect((effort as HTMLButtonElement).disabled).toBe(false);
    await user.click(effort);

    expect(onWrite).toHaveBeenNthCalledWith(2, { reasoning_effort: 'high' });
    expect(highlighted('opus')).toBe(true);
    expect(highlighted('chat.picker.effortOptions.high')).toBe(true);
  });

  it('shows the in-flight indicator on the trigger instead of greying the panel', async () => {
    const user = userEvent.setup();
    render(<OptimisticOwner onWrite={vi.fn()} />);
    expect(screen.queryByLabelText('common.saving')).toBeNull();

    await openMenu(user);
    await user.click(screen.getByRole('button', { name: 'opus' }));

    expect(screen.getByLabelText('common.saving')).toBeTruthy();
  });

  it('clears the effort when the catalog says the picked model has none', async () => {
    // "No efforts" is an answer the catalog gives, not a gap: the effort column
    // empties, so the route must stop carrying one instead of dispatching an
    // effort this model cannot run.
    catalogReasoning = { opus: [], sonnet: [{ value: 'low', label: 'Low' }] };
    const user = userEvent.setup();
    const onWrite = vi.fn();
    render(<OptimisticOwner onWrite={onWrite} />);
    await openMenu(user);

    await user.click(screen.getByRole('button', { name: 'opus' }));

    expect(onWrite).toHaveBeenCalledExactlyOnceWith({ model: 'opus', reasoning_effort: null });
    expect(screen.queryByRole('button', { name: 'chat.picker.effortOptions.low' })).toBeNull();
    // Two-column cascade: a heading with nothing under it would look like a
    // list that failed to load rather than a model that has no efforts.
    expect(screen.queryByText('chat.picker.effort')).toBeNull();
  });

  it('keeps the effort column for a model the catalog gives efforts', async () => {
    catalogReasoning = { opus: [{ value: 'low', label: 'Low' }] };
    const user = userEvent.setup();
    render(<OptimisticOwner onWrite={vi.fn()} />);
    await openMenu(user);

    await user.click(screen.getByRole('button', { name: 'opus' }));

    expect(screen.getByText('chat.picker.effort')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'chat.picker.effortOptions.low' })).toBeTruthy();
  });

  it('pins the whole inherited route when a model is picked on a default route', async () => {
    const user = userEvent.setup();
    const onWrite = vi.fn();
    render(
      <OptimisticOwner
        initial={{}}
        isDefaultRoute
        defaultRoute={CLAUDE_ROUTE}
        defaultLabel="default"
        onWrite={onWrite}
      />,
    );
    await openMenu(user);

    await user.click(screen.getByRole('button', { name: 'opus' }));

    // A bare {model} would leave the owner storing a model against no Agent.
    expect(onWrite).toHaveBeenCalledExactlyOnceWith({
      agent_name: 'claude',
      agent_id: 'agt-claude',
      agent_backend: 'claude',
      agent_variant: 'claude',
      model: 'opus',
      reasoning_effort: 'low',
    });
  });
});
