/* @vitest-environment jsdom */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

import { NewAgentDialog } from './NewAgentDialog';

const apiRef = vi.hoisted(() => ({ current: null as { createVibeAgent: ReturnType<typeof vi.fn> } | null }));

vi.stubGlobal('ResizeObserver', class {
  observe() {}
  unobserve() {}
  disconnect() {}
});

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
  render(<NewAgentDialog open onClose={vi.fn()} onCreated={vi.fn()} />);
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
});
