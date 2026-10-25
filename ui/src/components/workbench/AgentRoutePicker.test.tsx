/* @vitest-environment jsdom */

import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, within } from '@testing-library/react';
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
  // The key, plus the Source names a hint interpolates, so a test can read both.
  useTranslation: () => ({
    t: (key: string, options?: { sources?: string }) => (options?.sources ? `${key}:${options.sources}` : key),
  }),
}));

// What the Sources read answers: names by id, or null when the role may not read them.
let sourceNames: Record<string, string> | null = null;
vi.mock('../../lib/useSourceNames', () => ({ useSourceNames: () => sourceNames }));

// One client for every render, as the real context provides: the picker's
// catalog read keys on it, so a fresh object per render would re-read on its own.
// The Hub read answers per test; `useAddModelPath` owns what it means.
let hubMode: 'hub' | 'direct' = 'direct';
// Whether the next catalog read finds Model Hub unreadable (the native list, marked unread).
let hubUnread = false;
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
let catalogMenus: Record<string, { omitted: string[]; not_on: Record<string, string[]> }> = {};
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
      effortMenus?: Record<string, { omitted: string[]; not_on: Record<string, string[]> }>;
      hubUnread?: true;
      catalogRefreshPending: boolean;
    }) => void,
  ) => {
    catalogReads += 1;
    const answer = () => onLoaded({
      models: ['sonnet', 'opus'],
      modelLabels: {},
      reasoningOptions: catalogReasoning,
      // As the real loader answers: only a Hub catalog carries effort menus.
      ...(hubUnread ? { hubUnread: true as const } : hubMode === 'hub' ? { effortMenus: catalogMenus } : {}),
      catalogRefreshPending: false,
    });
    if (deferReads) pendingReads.push(answer);
    else answer();
    return () => {};
  },
}));

/** The open menu's effort column: the trigger also names the current effort. */
const effortColumn = () => within(screen.getByText('chat.picker.effort').parentElement as HTMLElement);

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
  presentation?: 'columns' | 'compact';
  onWrite: (patch: AgentRoutePatch) => void;
}> = ({ initial = CLAUDE_ROUTE, isDefaultRoute, defaultRoute, defaultLabel, presentation, onWrite }) => {
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
        presentation={presentation}
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
  await user.click(screen.getByRole('button', { name: /claude/i }));
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
  onChange?: (value: AgentRouteValue) => void;
  presentation?: 'columns' | 'compact';
}> = ({ role, surfaceActive = true, onNavigateAway, onChange = () => {}, presentation }) => (
  <InstanceAuthorizationContext.Provider
    value={{ remote: false, instanceKind: null, instanceRole: role, capabilities: capabilitiesFor(role) }}
  >
    <RouteSurfaceActiveContext.Provider value={surfaceActive}>
      <MemoryRouter>
        <AgentRoutePicker
          value={CLAUDE_ROUTE}
          agents={AGENTS}
          onChange={onChange}
          onNavigateAway={onNavigateAway}
          presentation={presentation}
        />
        <LocationProbe />
      </MemoryRouter>
    </RouteSurfaceActiveContext.Provider>
  </InstanceAuthorizationContext.Provider>
);

describe('AgentRoutePicker', () => {
  afterEach(() => {
    cleanup();
    catalogReasoning = {};
    catalogMenus = {};
    sourceNames = null;
    hubMode = 'direct';
    hubUnread = false;
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

  it('MH-EFFORT-MENU-002: names the Sources that refuse an effort, and says some do when it cannot read them', async () => {
    hubMode = 'hub';
    catalogReasoning = { sonnet: [{ value: 'low', label: 'low' }, { value: 'high', label: 'high' }] };
    catalogMenus = { sonnet: { omitted: [], not_on: { high: ['src_relaygpt1'] } } };
    sourceNames = { src_relaygpt1: 'relay-gpt' };
    const user = userEvent.setup();
    render(<HubPicker role="owner" />);
    await openMenu(user);

    expect(effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.high/ }).textContent)
      .toContain('chat.picker.effortNotOn:relay-gpt');
    // An effort every hop serves carries no hint.
    expect(effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.low/ }).textContent)
      .toBe('chat.picker.effortOptions.low');
    cleanup();

    // A role that may not read Sources still learns that some refuse it.
    sourceNames = null;
    render(<HubPicker role="owner" />);
    await openMenu(user);
    expect(effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.high/ }).textContent)
      .toContain('chat.picker.effortNotOnSome');
  });

  it('MH-EFFORT-MENU-003: keeps a saved effort routing now refuses everywhere visible as itself, and says so', async () => {
    // The route holds `low`; Model Hub now leaves it out because every hop refuses it.
    hubMode = 'hub';
    catalogReasoning = { sonnet: [{ value: 'high', label: 'high' }] };
    catalogMenus = { sonnet: { omitted: ['low'], not_on: {} } };
    const user = userEvent.setup();
    render(<HubPicker role="owner" />);
    await openMenu(user);

    const saved = effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.low/ });
    expect(saved.textContent).toContain('chat.picker.effortRefused');
    expect(saved.className).toContain('text-cyan-ink');
    // What routing serves is still offered, without the mark.
    expect(effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.high/ }).textContent)
      .toBe('chat.picker.effortOptions.high');
  });

  it('keeps the effort column in its own grid column when the refused saved effort is its only item', async () => {
    hubMode = 'hub';
    catalogReasoning = { sonnet: [] };
    catalogMenus = { sonnet: { omitted: ['low'], not_on: {} } };
    const user = userEvent.setup();
    render(<HubPicker role="owner" />);
    await openMenu(user);

    const column = screen.getByText('chat.picker.effort').parentElement as HTMLElement;
    expect((column.parentElement as HTMLElement).className).toContain('sm:grid-cols-[1fr_1.35fr_0.7fr]');
  });

  it('reads a refusal only from the effort\'s own entry, never one an effort name inherits', async () => {
    // Efforts are free-form: one named like an Object property has no entry of its own.
    hubMode = 'hub';
    catalogReasoning = { sonnet: [{ value: 'constructor', label: 'constructor' }, { value: 'high', label: 'high' }] };
    catalogMenus = { sonnet: { omitted: [], not_on: { high: ['src_relaygpt1'] } } };
    sourceNames = { src_relaygpt1: 'relay-gpt' };
    const user = userEvent.setup();
    render(<HubPicker role="owner" />);
    await openMenu(user);

    expect(effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.constructor/ }).textContent)
      .toBe('chat.picker.effortOptions.constructor');
    expect(effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.high/ }).textContent)
      .toContain('chat.picker.effortNotOn:relay-gpt');
  });

  it('reads a Hub catalog again on every open, since its effort menu follows evidence any turn can change', async () => {
    hubMode = 'hub';
    catalogReasoning = { sonnet: [{ value: 'low', label: 'low' }, { value: 'high', label: 'high' }] };
    const user = userEvent.setup();
    render(<HubPicker role="owner" />);
    await openMenu(user);
    expect(catalogReads).toBe(1);
    await user.keyboard('{Escape}');

    // A turn recorded that every hop refuses `high`.
    catalogReasoning = { sonnet: [{ value: 'low', label: 'low' }] };
    catalogMenus = { sonnet: { omitted: ['high'], not_on: {} } };
    await openMenu(user);
    expect(catalogReads).toBe(2);
    expect(effortColumn().queryByRole('button', { name: /chat\.picker\.effortOptions\.high/ })).toBeNull();
  });

  it('MH-EFFORT-MENU-004: holds the efforts and models of the last answer while the reopened menu reads again', async () => {
    hubMode = 'hub';
    catalogReasoning = { sonnet: [{ value: 'low', label: 'low' }, { value: 'high', label: 'high' }] };
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<HubPicker role="owner" onChange={onChange} />);
    await openMenu(user);
    await user.keyboard('{Escape}');

    // A turn recorded that every hop refuses `high`, and the reopened menu's read is still out.
    catalogReasoning = { sonnet: [{ value: 'low', label: 'low' }] };
    catalogMenus = { sonnet: { omitted: ['high'], not_on: {} } };
    deferReads = true;
    await openMenu(user);
    const high = effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.high/ }) as HTMLButtonElement;
    expect(high.disabled).toBe(true);
    await user.click(high);
    // A model choice keeps or clears the effort, so it waits for the answer too.
    const opus = screen.getByRole('button', { name: 'opus' }) as HTMLButtonElement;
    expect(opus.disabled).toBe(true);
    await user.click(opus);
    expect(onChange).not.toHaveBeenCalled();

    act(() => pendingReads.shift()!());
    expect(effortColumn().queryByRole('button', { name: /chat\.picker\.effortOptions\.high/ })).toBeNull();
  });

  it('keeps the last Hub answer, and reads Hub again, when one open falls back to the native list', async () => {
    // Hub: every hop refuses the route's `low`.
    hubMode = 'hub';
    catalogReasoning = { sonnet: [{ value: 'high', label: 'high' }] };
    catalogMenus = { sonnet: { omitted: ['low'], not_on: {} } };
    const user = userEvent.setup();
    render(<HubPicker role="owner" />);
    await openMenu(user);
    await user.keyboard('{Escape}');

    // This open's Hub read failed over to the native ladder, which offers `low` and
    // says nothing about Hub routing: the Hub answer stands.
    hubUnread = true;
    catalogReasoning = { sonnet: [{ value: 'low', label: 'low' }, { value: 'high', label: 'high' }] };
    await openMenu(user);
    expect(catalogReads).toBe(2);
    expect(effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.low/ }).textContent)
      .toContain('chat.picker.effortRefused');
    await user.keyboard('{Escape}');

    // Hub answers again, and the next open still asks it.
    hubUnread = false;
    await openMenu(user);
    expect(catalogReads).toBe(3);
  });

  it('takes a confirmed Direct answer over an earlier Hub one', async () => {
    hubMode = 'hub';
    catalogReasoning = { sonnet: [{ value: 'high', label: 'high' }] };
    catalogMenus = { sonnet: { omitted: ['low'], not_on: {} } };
    const user = userEvent.setup();
    render(<HubPicker role="owner" />);
    await openMenu(user);
    await user.keyboard('{Escape}');

    // Another tab switched the backend to Direct: its native list is the answer now.
    hubMode = 'direct';
    catalogReasoning = { sonnet: [{ value: 'low', label: 'low' }, { value: 'high', label: 'high' }] };
    await openMenu(user);
    expect(effortColumn().getByRole('button', { name: /chat\.picker\.effortOptions\.low/ }).textContent)
      .toBe('chat.picker.effortOptions.low');
    await user.keyboard('{Escape}');

    // And it is cached like any Direct list again: reopening reads nothing (a native
    // read may start a CLI).
    const reads = catalogReads;
    await openMenu(user);
    expect(catalogReads).toBe(reads);
  });

  it('keeps a refused saved effort when the current model is chosen again', async () => {
    hubMode = 'hub';
    catalogReasoning = { sonnet: [{ value: 'high', label: 'high' }] };
    catalogMenus = { sonnet: { omitted: ['low'], not_on: {} } };
    const onChange = vi.fn();
    const user = userEvent.setup();
    render(<HubPicker role="owner" onChange={onChange} />);
    await openMenu(user);

    await user.click(screen.getByRole('button', { name: 'sonnet' }));
    // Nothing changed, so nothing clears the effort the route holds.
    expect(onChange.mock.calls.map(([value]) => value.reasoning_effort)).not.toContain(null);
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

// The chat composer's presentation: the same route state and writes, laid out
// model-first — models and an effort radio group up front, the Agent list one
// level down — so the picks most made mid-conversation take one tap.
describe('AgentRoutePicker compact presentation', () => {
  afterEach(() => {
    cleanup();
    catalogReasoning = {};
    catalogMenus = {};
    sourceNames = null;
    hubMode = 'direct';
  });

  const effortGroup = () => within(screen.getByRole('radiogroup', { name: 'chat.picker.effort' }));

  it('writes models and efforts from the first page and keeps Agents one level down', async () => {
    catalogReasoning = {
      sonnet: [{ value: 'low', label: 'Low' }, { value: 'high', label: 'High' }],
      opus: [{ value: 'low', label: 'Low' }, { value: 'high', label: 'High' }],
    };
    const user = userEvent.setup();
    const onWrite = vi.fn();
    render(<OptimisticOwner presentation="compact" onWrite={onWrite} />);
    await openMenu(user);

    // Agents are not on the first page; the route's Agent is named on its row.
    expect(screen.queryByRole('button', { name: /codex/ })).toBeNull();
    expect(screen.queryByRole('button', { name: 'chat.picker.newAgent' })).toBeNull();
    expect(effortGroup().getByRole('radio', { name: 'chat.picker.effortOptions.low' }).getAttribute('aria-checked')).toBe('true');

    await user.click(effortGroup().getByRole('radio', { name: 'chat.picker.effortOptions.high' }));
    expect(onWrite).toHaveBeenLastCalledWith({ reasoning_effort: 'high' });
    expect(effortGroup().getByRole('radio', { name: 'chat.picker.effortOptions.high' }).getAttribute('aria-checked')).toBe('true');

    await user.click(screen.getByRole('button', { name: /^opus/ }));
    expect(onWrite).toHaveBeenLastCalledWith({ model: 'opus' });

    await user.click(screen.getByRole('button', { name: /chat\.picker\.agent/ }));
    await user.click(screen.getByRole('button', { name: /^codex/ }));
    // An Agent pick is the whole route, exactly as the cascade writes it, and
    // the menu returns to its model page.
    expect(onWrite).toHaveBeenLastCalledWith({
      agent_name: 'codex',
      agent_id: 'agt-codex',
      agent_backend: 'codex',
      agent_variant: 'codex',
      model: 'gpt',
      reasoning_effort: null,
    });
    expect(screen.getByText('chat.picker.model')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'chat.picker.newAgent' })).toBeNull();
  });

  it('still names refusing Sources and a refused saved effort, as the cascade does', async () => {
    hubMode = 'hub';
    catalogReasoning = { sonnet: [{ value: 'medium', label: 'medium' }, { value: 'high', label: 'high' }] };
    catalogMenus = { sonnet: { omitted: ['low'], not_on: { high: ['src_relaygpt1'] } } };
    sourceNames = { src_relaygpt1: 'relay-gpt' };
    const user = userEvent.setup();
    render(<HubPicker role="owner" presentation="compact" />);
    await openMenu(user);

    // The saved `low` every hop now refuses stays checked as itself, with the reason.
    expect(effortGroup().getByRole('radio', { name: 'chat.picker.effortOptions.low' }).getAttribute('aria-checked')).toBe('true');
    expect(screen.getByText('chat.picker.effortRefused')).toBeTruthy();
    // A Source that refuses an offered effort is named under the group and on the option.
    const hint = screen.getByText('chat.picker.effortNotOn:relay-gpt', { selector: 'p > span' });
    expect(hint.parentElement?.textContent).toBe('chat.picker.effortOptions.highchat.picker.effortNotOn:relay-gpt');
    expect(effortGroup().getByRole('radio', { name: 'chat.picker.effortOptions.high' }).getAttribute('title'))
      .toBe('chat.picker.effortNotOn:relay-gpt');
    expect(effortGroup().getByRole('radio', { name: 'chat.picker.effortOptions.medium' }).getAttribute('title')).toBeNull();
  });
});
