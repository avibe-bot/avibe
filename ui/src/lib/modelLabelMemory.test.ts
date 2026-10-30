/* @vitest-environment jsdom */

// A closed picker names its model ("Opus 5.5") instead of the stored id. The
// cost contract: a label this browser has seen costs no request, a miss costs
// one read per backend per load of what is already on disk, and OpenCode's live
// list — which may start OpenCode — is never read for a label.

import { renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const api = {
  readModelHubAgentCatalogForModelPicker: vi.fn(),
  claudeModels: vi.fn(),
  codexModels: vi.fn(),
  readOpencodeOptionsForModelPicker: vi.fn(),
};
vi.mock('../context/ApiContext', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../context/ApiContext')>()),
  useApi: () => api,
}));

const STORAGE_KEY = 'avibe.model-labels.v1';

// The memory is page-load state: each case loads a fresh module, as a reload would.
const freshMemory = async () => {
  vi.resetModules();
  return import('./modelLabelMemory');
};

beforeEach(() => {
  window.localStorage.clear();
  for (const fn of Object.values(api)) fn.mockReset();
  api.readModelHubAgentCatalogForModelPicker.mockResolvedValue(null);
  api.claudeModels.mockResolvedValue({
    ok: true,
    models: ['claude-opus-5-5', 'claude-sonnet-5-5'],
    model_labels: { 'claude-opus-5-5': 'Opus 5.5', 'claude-sonnet-5-5': 'Sonnet 5.5' },
  });
});

describe('remembered model labels', () => {
  it('names a model seen in an earlier load from the first render, without a request', async () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ claude: { 'claude-opus-5-5': 'Opus 5.5' } }));
    const { useRememberedModelLabel } = await freshMemory();

    const { result } = renderHook(() => useRememberedModelLabel('claude', 'claude-opus-5-5'));

    expect(result.current).toBe('Opus 5.5');
    expect(api.readModelHubAgentCatalogForModelPicker).not.toHaveBeenCalled();
    expect(api.claudeModels).not.toHaveBeenCalled();
  });

  it('reads a backend once per load on a miss, then names every model of it', async () => {
    const { useRememberedModelLabel } = await freshMemory();

    const opus = renderHook(() => useRememberedModelLabel('claude', 'claude-opus-5-5'));
    const sonnet = renderHook(() => useRememberedModelLabel('claude', 'claude-sonnet-5-5'));
    // A model the catalog has no label for keeps its id, and asks nothing more.
    const custom = renderHook(() => useRememberedModelLabel('claude', 'my-custom-model'));

    await waitFor(() => expect(opus.result.current).toBe('Opus 5.5'));
    expect(sonnet.result.current).toBe('Sonnet 5.5');
    expect(custom.result.current).toBeNull();
    expect(api.readModelHubAgentCatalogForModelPicker).toHaveBeenCalledTimes(1);
    expect(api.claudeModels).toHaveBeenCalledTimes(1);
    // ...and the next load names them without asking.
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}').claude['claude-opus-5-5']).toBe('Opus 5.5');
  });

  it('takes a Model Hub catalog\'s display names, and never reads OpenCode\'s live list', async () => {
    api.readModelHubAgentCatalogForModelPicker.mockImplementation(async (backend: string) =>
      backend === 'codex'
        ? {
          backend: 'codex',
          mode: 'hub',
          catalog_models: [{ id: 'gpt-6.1', display_name: 'GPT-6.1', routeable: true, reasoning_efforts: [], supports_reasoning: true }],
        }
        : null,
    );
    const { useRememberedModelLabel } = await freshMemory();

    const codex = renderHook(() => useRememberedModelLabel('codex', 'gpt-6.1'));
    const opencode = renderHook(() => useRememberedModelLabel('opencode', 'anthropic/claude-opus-5-5'));

    await waitFor(() => expect(codex.result.current).toBe('GPT-6.1'));
    await waitFor(() => expect(api.readModelHubAgentCatalogForModelPicker).toHaveBeenCalledWith('opencode'));
    expect(opencode.result.current).toBeNull();
    expect(api.codexModels).not.toHaveBeenCalled();
    expect(api.readOpencodeOptionsForModelPicker).not.toHaveBeenCalled();
  });
});

describe('remembered fast-tier models', () => {
  const hubCatalog = (fast: string[], unconfirmed: string[] = []) => ({
    backend: 'claude',
    mode: 'hub',
    catalog_models: ['claude-opus-5-5', 'claude-sonnet-5-5'].map((id) => ({
      id, origin: 'builtin', display_name: null, models_dev_id: null, context_window: null, max_output_tokens: null,
      input_modalities: [], output_modalities: [], supports_tools: null, supports_reasoning: null, reasoning_efforts: [],
      locked: false, routeable: true,
      fast_mode: fast.includes(id) || unconfirmed.includes(id),
      fast_mode_confirmed: unconfirmed.includes(id) ? false : null,
    })),
  });
  const STORED = 'avibe.fast-models.v2';

  it('paints from a stored answer, then reads it again once per load since routing may have moved', async () => {
    // Stored while the route went through a Source without the tier.
    window.localStorage.setItem(STORED, JSON.stringify({ claude: { offered: [], unconfirmed: [] } }));
    api.readModelHubAgentCatalogForModelPicker.mockResolvedValue(hubCatalog(['claude-opus-5-5']));
    const { useRememberedFastModel } = await freshMemory();

    const { result, rerender } = renderHook(({ wanted }) => useRememberedFastModel('claude', 'claude-opus-5-5', wanted), {
      initialProps: { wanted: false },
    });
    expect(result.current).toBeNull();
    expect(api.readModelHubAgentCatalogForModelPicker).not.toHaveBeenCalled();

    rerender({ wanted: true });
    await waitFor(() => expect(result.current).toBe('offered'));
    rerender({ wanted: true });
    expect(api.readModelHubAgentCatalogForModelPicker).toHaveBeenCalledTimes(1);
  });

  it('follows what the last fast response said, read again when a fast turn ends', async () => {
    api.readModelHubAgentCatalogForModelPicker.mockResolvedValue(hubCatalog(['claude-opus-5-5']));
    const { useRememberedFastModel, refreshFastModels } = await freshMemory();
    const { result } = renderHook(() => useRememberedFastModel('claude', 'claude-opus-5-5', true));
    await waitFor(() => expect(result.current).toBe('offered'));

    // The turn ran through a relay that answered at the standard tier.
    api.readModelHubAgentCatalogForModelPicker.mockResolvedValue(hubCatalog([], ['claude-opus-5-5']));
    refreshFastModels(api as never, 'claude');
    await waitFor(() => expect(result.current).toBe('unconfirmed'));
    expect(JSON.parse(window.localStorage.getItem(STORED) ?? '{}')).toEqual({
      claude: { offered: [], unconfirmed: ['claude-opus-5-5'] },
    });

    // A later fast turn there ran fast.
    api.readModelHubAgentCatalogForModelPicker.mockResolvedValue(hubCatalog(['claude-opus-5-5']));
    refreshFastModels(api as never, 'claude');
    await waitFor(() => expect(result.current).toBe('offered'));
  });
});
