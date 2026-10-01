/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { RouteSurfaceActiveContext } from '../../lib/routeSurfaceActivity';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { NewAgentDialog } from './NewAgentDialog';

const apiRef = vi.hoisted(() => ({
  current: null as {
    createVibeAgent: ReturnType<typeof vi.fn>;
    readModelHubAgentCatalogForModelPicker?: ReturnType<typeof vi.fn>;
  } | null,
}));

vi.stubGlobal('ResizeObserver', class {
  observe() {}
  unobserve() {}
  disconnect() {}
});
// cmdk scrolls its highlighted row into view; jsdom implements no scrolling.
Element.prototype.scrollIntoView = vi.fn();

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) => (values ? `${key}:${JSON.stringify(values)}` : key),
  }),
}));

vi.mock('../../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../context/ApiContext')>()),
  useApi: () => apiRef.current,
}));

// What the catalog answers for this render: a Hub catalog states one entry per
// model, so a test can say "this model has no efforts" the way the server does.
type FakeModelCatalog = {
  models: string[];
  reasoningOptions?: Record<string, { value: string; label: string }[]>;
};
let modelCatalog: FakeModelCatalog = { models: [] };
let modelCatalogReads = 0;
let deferReads = false;
let failReads = false;
const pendingReads: (() => void)[] = [];

vi.mock('../../lib/backendModels', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/backendModels')>()),
  loadBackendModelsWithRefresh: (
    _api: unknown,
    _backend: string,
    onLoaded: (payload: FakeModelCatalog) => void,
    onInitialError?: () => void,
  ) => {
    modelCatalogReads += 1;
    if (failReads) {
      onInitialError?.();
      return () => {};
    }
    const catalog = modelCatalog;
    if (deferReads) pendingReads.push(() => onLoaded(catalog));
    else onLoaded(catalog);
    return () => {};
  },
}));

const renderDialog = () => {
  const createVibeAgent = vi.fn().mockResolvedValue({ ok: true, agent: { id: 'agt-new' } });
  apiRef.current = { createVibeAgent };
  render(
    <MemoryRouter>
      <NewAgentDialog open onClose={vi.fn()} onCreated={vi.fn()} />
    </MemoryRouter>,
  );
  return { createVibeAgent };
};

const chooseModel = (value: string) => {
  fireEvent.click(screen.getByRole('combobox'));
  fireEvent.change(screen.getByPlaceholderText('Search...'), { target: { value } });
  fireEvent.click(screen.getByText(`Use "${value}"`));
};

const submit = () => fireEvent.click(screen.getByRole('button', { name: /agents\.create\.submit/ }));

afterEach(() => {
  cleanup();
  apiRef.current = null;
  modelCatalog = { models: [] };
  modelCatalogReads = 0;
  deferReads = false;
  failReads = false;
  pendingReads.length = 0;
});

describe('NewAgentDialog', () => {
  it("keeps the form while the user adds a model in the Model Hub catalog", async () => {
    // Settings hides the page this dialog sits in; closing it would clear the
    // Agent the user was halfway through defining.
    modelCatalog = { models: ['opus'] };
    apiRef.current = {
      createVibeAgent: vi.fn(),
      readModelHubAgentCatalogForModelPicker: vi.fn().mockResolvedValue({ backend: 'claude', mode: 'hub' }),
    };
    const onClose = vi.fn();
    const LocationProbe = () => {
      const location = useLocation();
      return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
    };
    render(
      <InstanceAuthorizationContext.Provider
        value={{ remote: false, instanceKind: null, instanceRole: 'owner', capabilities: OWNER_INSTANCE_CAPABILITIES }}
      >
        <MemoryRouter>
          <NewAgentDialog open onClose={onClose} onCreated={vi.fn()} />
          <LocationProbe />
        </MemoryRouter>
      </InstanceAuthorizationContext.Provider>,
    );
    fireEvent.change(screen.getByPlaceholderText('agents.create.namePlaceholder'), { target: { value: 'router' } });

    fireEvent.click(screen.getByRole('combobox'));
    fireEvent.click(await screen.findByText('chat.picker.addModel'));

    expect(screen.getByTestId('location').textContent).toBe('/settings/models?manage=claude');
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue('router')).toBeTruthy();
  });

  it('reads the model list again when Settings uncovers the open dialog', () => {
    // Closing and reopening would refresh it too, but only by clearing the form.
    apiRef.current = { createVibeAgent: vi.fn() };
    const dialog = (surfaceActive: boolean) => (
      <RouteSurfaceActiveContext.Provider value={surfaceActive}>
        <MemoryRouter>
          <NewAgentDialog open onClose={vi.fn()} onCreated={vi.fn()} />
        </MemoryRouter>
      </RouteSurfaceActiveContext.Provider>
    );
    const { rerender } = render(dialog(true));
    const reads = modelCatalogReads;

    rerender(dialog(false));
    rerender(dialog(true));

    expect(modelCatalogReads).toBe(reads + 1);
  });

  it('falls back to the backend ladder, not the efforts read before a Settings visit, when the read after it fails', () => {
    // The visit may have removed `max` from this model; a failed read cannot say
    // it did not, so the pre-visit efforts must not come back as choices.
    modelCatalog = { models: [], reasoningOptions: { 'only-max': [{ value: 'max', label: 'Max' }] } };
    apiRef.current = { createVibeAgent: vi.fn() };
    const dialog = (surfaceActive: boolean) => (
      <RouteSurfaceActiveContext.Provider value={surfaceActive}>
        <MemoryRouter>
          <NewAgentDialog open onClose={vi.fn()} onCreated={vi.fn()} />
        </MemoryRouter>
      </RouteSurfaceActiveContext.Provider>
    );
    const { rerender } = render(dialog(true));
    chooseModel('only-max');
    expect(screen.getByRole('button', { name: 'max', exact: true })).toBeTruthy();

    failReads = true;
    rerender(dialog(false));
    rerender(dialog(true));

    expect(screen.queryByRole('button', { name: 'max', exact: true })).toBeNull();
    expect(screen.getByRole('button', { name: 'medium', exact: true })).toBeTruthy();
  });

  it('offers no model read before a Settings visit while the read after it is in flight', () => {
    modelCatalog = { models: ['opus'] };
    apiRef.current = { createVibeAgent: vi.fn() };
    const dialog = (surfaceActive: boolean) => (
      <RouteSurfaceActiveContext.Provider value={surfaceActive}>
        <MemoryRouter>
          <NewAgentDialog open onClose={vi.fn()} onCreated={vi.fn()} />
        </MemoryRouter>
      </RouteSurfaceActiveContext.Provider>
    );
    const { rerender } = render(dialog(true));
    fireEvent.change(screen.getByPlaceholderText('agents.create.namePlaceholder'), { target: { value: 'router' } });
    const create = () => screen.getByRole('button', { name: /agents\.create\.submit/, hidden: true }) as HTMLButtonElement;
    const medium = () => screen.getByRole('button', { name: 'medium', exact: true, hidden: true }) as HTMLButtonElement;
    expect(create().disabled).toBe(false);
    deferReads = true;
    rerender(dialog(false));
    rerender(dialog(true));

    // The draft keeps its effort, but neither it nor Create can be chosen until
    // the new read can say whether the model still takes it.
    expect(screen.getByDisplayValue('router')).toBeTruthy();
    expect(medium().disabled).toBe(true);
    expect(create().disabled).toBe(true);
    fireEvent.click(screen.getByRole('combobox'));
    expect(screen.queryByText('opus')).toBeNull();
    act(() => pendingReads.shift()!());
    expect(screen.getByText('opus')).toBeTruthy();
    expect(medium().disabled).toBe(false);
    expect(create().disabled).toBe(false);
  });

  it('creates with no effort when the catalog says the model has none', async () => {
    // `medium` is only a starting suggestion. Sending it for a model whose
    // catalog row states no efforts would create an Agent whose very first
    // dispatch carries a parameter the model cannot take.
    modelCatalog = { models: [], reasoningOptions: { 'no-effort-model': [] } };
    const { createVibeAgent } = renderDialog();

    fireEvent.change(screen.getByPlaceholderText('agents.create.namePlaceholder'), { target: { value: 'router' } });
    chooseModel('no-effort-model');

    // The whole field goes and the model takes the row: an empty outline under a
    // heading reads as a control that failed to load.
    expect(screen.queryByRole('button', { name: 'medium', exact: true })).toBeNull();
    expect(screen.queryByText('agents.detail.effort')).toBeNull();
    expect(screen.getByText('agents.create.model').parentElement?.className).toContain('col-span-2');
    submit();

    await waitFor(() => expect(createVibeAgent).toHaveBeenCalledWith(expect.objectContaining({
      name: 'router',
      model: 'no-effort-model',
      reasoning_effort: null,
    })));
  });

  it('still sends the suggested effort for a model that has one', async () => {
    modelCatalog = { models: [], reasoningOptions: { 'reasoning-model': [{ value: 'medium', label: 'Medium' }] } };
    const { createVibeAgent } = renderDialog();

    fireEvent.change(screen.getByPlaceholderText('agents.create.namePlaceholder'), { target: { value: 'router' } });
    chooseModel('reasoning-model');

    expect(screen.getByText('agents.detail.effort')).toBeTruthy();
    expect(screen.getByText('agents.create.model').parentElement?.className).not.toContain('col-span-2');
    submit();

    await waitFor(() => expect(createVibeAgent).toHaveBeenCalledWith(expect.objectContaining({
      model: 'reasoning-model',
      reasoning_effort: 'medium',
    })));
  });

  it('keeps the backend fallback for a model the catalog does not name', async () => {
    const { createVibeAgent } = renderDialog();

    fireEvent.change(screen.getByPlaceholderText('agents.create.namePlaceholder'), { target: { value: 'router' } });
    chooseModel('typed/unknown');
    expect(screen.getByRole('button', { name: 'medium', exact: true })).toBeTruthy();
    submit();

    await waitFor(() => expect(createVibeAgent).toHaveBeenCalledWith(expect.objectContaining({
      reasoning_effort: 'medium',
    })));
  });

  it.each([false, true])('keeps medium unless the user selects declared Off (selected=%s)', async (selectOff) => {
    modelCatalog = {
      models: [],
      reasoningOptions: {
        'with-off': [{ value: 'none', label: 'Off' }, { value: 'medium', label: 'Medium' }],
      },
    };
    const { createVibeAgent } = renderDialog();
    fireEvent.change(screen.getByPlaceholderText('agents.create.namePlaceholder'), { target: { value: 'router' } });
    chooseModel('with-off');
    const off = screen.getByRole('button', { name: 'chat.picker.effortOptions.none' });
    if (selectOff) fireEvent.click(off);
    submit();

    await waitFor(() => expect(createVibeAgent).toHaveBeenCalledWith(expect.objectContaining({
      model: 'with-off',
      reasoning_effort: selectOff ? 'none' : 'medium',
    })));
  });
});
