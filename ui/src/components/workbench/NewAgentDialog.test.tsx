/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { InstanceAuthorizationContext } from '../../context/InstanceAuthorizationContext';
import { OWNER_INSTANCE_CAPABILITIES } from '../../lib/sessionInfo';
import { NewAgentDialog } from './NewAgentDialog';

const apiRef = vi.hoisted(() => ({ current: null as { createVibeAgent: ReturnType<typeof vi.fn> } | null }));

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
  hubManaged?: boolean;
};
let modelCatalog: FakeModelCatalog = { models: [] };

vi.mock('../../lib/backendModels', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/backendModels')>()),
  loadBackendModelsWithRefresh: (_api: unknown, _backend: string, onLoaded: (payload: FakeModelCatalog) => void) => {
    onLoaded(modelCatalog);
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
});

describe('NewAgentDialog', () => {
  it("closes before leaving for the Model Hub catalog of a Hub backend's models", () => {
    // Left open, the dialog would sit over the page it sent the user to.
    modelCatalog = { models: ['opus'], hubManaged: true };
    apiRef.current = { createVibeAgent: vi.fn() };
    const events: string[] = [];
    const LocationProbe = () => {
      const location = useLocation();
      if (location.search) events.push(`navigate ${location.pathname}${location.search}`);
      return null;
    };
    render(
      <InstanceAuthorizationContext.Provider
        value={{ remote: false, instanceKind: null, instanceRole: 'owner', capabilities: OWNER_INSTANCE_CAPABILITIES }}
      >
        <MemoryRouter>
          <NewAgentDialog open onClose={() => events.push('close')} onCreated={vi.fn()} />
          <LocationProbe />
        </MemoryRouter>
      </InstanceAuthorizationContext.Provider>,
    );

    fireEvent.click(screen.getByRole('combobox'));
    fireEvent.click(screen.getByText('chat.picker.addModel'));

    expect(events).toEqual(['close', 'navigate /settings/models?manage=claude']);
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
