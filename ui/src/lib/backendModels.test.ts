import { afterEach, describe, expect, it, vi } from 'vitest';

import { blankBackendModel, candidateBackendModel } from '../components/settings/models/backendCatalog';
import type { AgentSupply, BackendModel } from '../components/settings/models/types';
import { ApiError, type ApiContextType } from '../context/ApiContext';
import { fetchBackendModels, loadBackendModelsWithRefresh, modelOptionLabel } from './backendModels';
import { resolveEffortOptions } from './effortOptions';

/** No Model Hub record to read, which is what every pre-Hub server, unreadable
 *  answer, and disabled engine amounts to for a picker. */
const noHubCatalog = () => vi.fn().mockResolvedValue(null);

const model = (id: string, overrides: Partial<BackendModel> = {}): BackendModel => ({
  ...blankBackendModel(),
  id,
  ...overrides,
});

const hubAgent = (
  backend: AgentSupply['backend'],
  overrides: Partial<AgentSupply> = {},
): AgentSupply => ({
  backend,
  cli_present: true,
  mode: 'hub',
  menu_kind: backend === 'opencode' ? 'open' : 'fixed',
  sources: { order: [], eligibility: [] },
  routes: {},
  menu: null,
  builtin_models: ['legacy-a', 'legacy-b'],
  ...overrides,
});

describe('fetchBackendModels for OpenCode', () => {
  it('reads the public options catalog without borrowing the native Settings surface', async () => {
    const readOpencodeOptionsForModelPicker = vi.fn().mockResolvedValue({
      ok: true,
      data: {
        models: {
          providers: [
            { id: 'openrouter', models: { 'anthropic/claude-x': {} } },
            { id: 'custom', models: [{ id: 'hub-only' }] },
          ],
        },
        reasoning_options: {
          'custom/hub-only': [{ value: 'high', label: 'High' }],
        },
      },
    });
    const getOpencodeProviders = vi.fn();
    const api = {
      readModelHubAgentCatalogForModelPicker: noHubCatalog(),
      readOpencodeOptionsForModelPicker,
      getOpencodeProviders,
    } as unknown as ApiContextType;

    const result = await fetchBackendModels(api, 'opencode');

    expect(getOpencodeProviders).not.toHaveBeenCalled();
    expect(readOpencodeOptionsForModelPicker).toHaveBeenCalledOnce();
    expect(result.models).toEqual([
      'openrouter/anthropic/claude-x',
      'custom/hub-only',
    ]);
    expect(result.reasoningOptions).toEqual({
      'custom/hub-only': [{ value: 'high', label: 'High' }],
    });
  });

  it('degrades to an empty catalog when the rank may not read the live options endpoint', async () => {
    const api = {
      readModelHubAgentCatalogForModelPicker: noHubCatalog(),
      readOpencodeOptionsForModelPicker: vi
        .fn()
        .mockRejectedValue(new ApiError('forbidden', 403, 'instance_access_forbidden')),
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'opencode')).resolves.toEqual({ models: [] });
  });

  it('still propagates a failure that is not the expected refusal', async () => {
    const api = {
      readModelHubAgentCatalogForModelPicker: noHubCatalog(),
      readOpencodeOptionsForModelPicker: vi
        .fn()
        .mockRejectedValue(new ApiError('boom', 500, null)),
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'opencode')).rejects.toBeInstanceOf(ApiError);
  });
});

describe('fetchBackendModels in gateway mode', () => {
  it('does not use inherited Object properties as model labels', () => {
    expect(modelOptionLabel('constructor', {})).toBe('constructor');
  });

  it('offers exactly the models the Model Hub catalog holds', async () => {
    const claudeModels = vi.fn();
    const api = {
      readModelHubAgentCatalogForModelPicker: vi.fn().mockResolvedValue(
        hubAgent('claude', {
          catalog_models: [
            // The backend's own selector: visible in the catalog, never a Route
            // key, so it is not a model anything can be pointed at.
            model('default', { locked: true, routeable: false }),
            model('alpha', {
              display_name: 'Alpha',
              supports_reasoning: true,
              reasoning_efforts: ['low', 'high'],
            }),
            model('beta'),
          ],
        }),
      ),
      claudeModels,
    } as unknown as ApiContextType;

    const result = await fetchBackendModels(api, 'claude');

    expect(claudeModels).not.toHaveBeenCalled();
    expect(result).toEqual({
      models: ['alpha', 'beta'],
      modelLabels: { alpha: 'Alpha' },
      reasoningOptions: {
        alpha: [{ value: 'low', label: 'low' }, { value: 'high', label: 'high' }],
        // Present and empty: `beta` states it does not reason, and only a key
        // the catalog never wrote may fall back to the generic ladder.
        beta: [],
      },
    });
  });

  it('states "no efforts" for every routeable model the catalog says does not reason', async () => {
    const api = {
      readModelHubAgentCatalogForModelPicker: vi.fn().mockResolvedValue(
        hubAgent('codex', {
          catalog_models: [
            // Efforts left behind by an earlier fill: the row now says it does
            // not reason, and the runtime drops the variants for exactly that
            // reason, so the picker must not offer them either.
            model('off-with-leftovers', { supports_reasoning: false, reasoning_efforts: ['low', 'high'] }),
            // A model that reasons but names no ladder — "omit the parameter".
            model('on-without-efforts', { supports_reasoning: true, reasoning_efforts: [] }),
            // Unset stays unset: the row never denies reasoning, so its own
            // empty list is what the picker reports.
            model('unset', { supports_reasoning: null, reasoning_efforts: [] }),
          ],
        }),
      ),
      codexModels: vi.fn(),
    } as unknown as ApiContextType;

    const result = await fetchBackendModels(api, 'codex');

    expect(result.reasoningOptions).toEqual({
      'off-with-leftovers': [],
      'on-without-efforts': [],
      unset: [],
    });
    // And the resolver reads those keys as an answer rather than a gap.
    for (const id of result.models) {
      expect(resolveEffortOptions('codex', id, result.reasoningOptions)).toEqual([]);
    }
  });

  it('keeps the efforts a picked candidate arrived with, all the way to the Route editor', async () => {
    // Built the way the picker builds it — a checkbox, no editor — so this is
    // the whole path from 「the server proposed these tiers」 to 「the Route
    // editor offers them」. A floor that answered `supports_reasoning: false`
    // on the row's behalf broke it here: the projection reads that as 「this
    // model does not reason」 and suppresses the very efforts the row was
    // created with, leaving a reasoning model with nothing to select.
    const picked = candidateBackendModel({
      id: 'glm-5.2',
      display_name: 'GLM 5.2',
      reasoning_efforts: ['low', 'high'],
      suppliers: [{ source_id: 'src_relay0001', source_name: 'relay.example', model_id: 'glm-5.2-air' }],
      origin: 'provider',
    });
    const api = {
      readModelHubAgentCatalogForModelPicker: vi.fn().mockResolvedValue(
        hubAgent('codex', { catalog_models: [picked] }),
      ),
      codexModels: vi.fn(),
    } as unknown as ApiContextType;

    // The row states no capability at all: `null` leaves the backend's own
    // default in force, which is the only honest reading of a row nobody
    // opened.
    expect(picked.supports_reasoning).toBeNull();
    expect(picked.supports_tools).toBeNull();

    const result = await fetchBackendModels(api, 'codex');

    expect(resolveEffortOptions('codex', 'glm-5.2', result.reasoningOptions)).toEqual(['low', 'high']);
  });

  it('treats model ids as data rather than Object properties', async () => {
    const api = {
      readModelHubAgentCatalogForModelPicker: vi.fn().mockResolvedValue(
        hubAgent('codex', {
          catalog_models: [
            model('constructor', {
              display_name: 'Constructor',
              supports_reasoning: true,
              reasoning_efforts: ['high'],
            }),
            model('__proto__', { display_name: 'Prototype', reasoning_efforts: [] }),
          ],
        }),
      ),
      codexModels: vi.fn(),
    } as unknown as ApiContextType;

    const result = await fetchBackendModels(api, 'codex');

    expect(result.models).toEqual(['constructor', '__proto__']);
    expect(result.modelLabels.constructor).toBe('Constructor');
    expect(result.modelLabels.__proto__).toBe('Prototype');
    expect(Object.prototype.hasOwnProperty.call(result.modelLabels, '__proto__')).toBe(true);
    expect(resolveEffortOptions('codex', 'constructor', result.reasoningOptions)).toEqual(['high']);
    expect(resolveEffortOptions('codex', '__proto__', result.reasoningOptions)).toEqual([]);
  });

  it('picks up a model the user just added and drops one they removed', async () => {
    const readModelHubAgentCatalogForModelPicker = vi
      .fn()
      .mockResolvedValueOnce(hubAgent('codex', { catalog_models: [model('kept'), model('removed')] }))
      .mockResolvedValueOnce(hubAgent('codex', { catalog_models: [model('kept'), model('added')] }));
    const api = {
      readModelHubAgentCatalogForModelPicker,
      codexModels: vi.fn(),
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'codex')).resolves.toMatchObject({
      models: ['kept', 'removed'],
    });
    await expect(fetchBackendModels(api, 'codex')).resolves.toMatchObject({
      models: ['kept', 'added'],
    });
  });

  it('keeps an emptied catalog empty instead of reopening the backend list', async () => {
    const codexModels = vi.fn().mockResolvedValue({ ok: true, models: ['gpt-old'] });
    const api = {
      readModelHubAgentCatalogForModelPicker: vi
        .fn()
        .mockResolvedValue(hubAgent('codex', { catalog_models: [] })),
      codexModels,
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'codex')).resolves.toEqual({
      models: [],
      modelLabels: {},
      reasoningOptions: {},
    });
    expect(codexModels).not.toHaveBeenCalled();
  });

  it('never starts OpenCode to list models the catalog already names', async () => {
    const readOpencodeOptionsForModelPicker = vi.fn();
    const api = {
      readModelHubAgentCatalogForModelPicker: vi.fn().mockResolvedValue(
        hubAgent('opencode', { catalog_models: [model('openrouter/anthropic/claude-x')] }),
      ),
      readOpencodeOptionsForModelPicker,
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'opencode')).resolves.toMatchObject({
      models: ['openrouter/anthropic/claude-x'],
      // OpenCode has no shared default set to fall back to, so its per-model
      // answer has to arrive as an answer rather than as a missing key.
      reasoningOptions: { 'openrouter/anthropic/claude-x': [] },
    });
    expect(readOpencodeOptionsForModelPicker).not.toHaveBeenCalled();
  });
});

describe('fetchBackendModels outside gateway mode', () => {
  it('uses the backend own catalog in direct mode, whatever the Hub has stored', async () => {
    const claudeModels = vi.fn().mockResolvedValue({
      ok: true,
      models: ['native-a'],
      model_labels: { 'native-a': 'Native A' },
    });
    const api = {
      readModelHubAgentCatalogForModelPicker: vi.fn().mockResolvedValue(
        // A direct backend reaches its provider itself, so this stored catalog
        // describes a gateway that is not in the path.
        hubAgent('claude', { mode: 'direct', routes: null, catalog_models: [model('hub-only')] }),
      ),
      claudeModels,
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'claude')).resolves.toMatchObject({
      models: ['native-a'],
      modelLabels: { 'native-a': 'Native A' },
    });
    expect(claudeModels).toHaveBeenCalledOnce();
  });

  it('uses the backend own catalog while a server predating the Hub catalog answers', async () => {
    const claudeModels = vi.fn().mockResolvedValue({ ok: true, models: ['native-a'] });
    const api = {
      // Hub mode, and no `catalog_models` key at all — the rolling-upgrade
      // window, where a synthesized list would be this client's invention.
      readModelHubAgentCatalogForModelPicker: vi.fn().mockResolvedValue(hubAgent('claude')),
      claudeModels,
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'claude')).resolves.toMatchObject({ models: ['native-a'] });
    expect(claudeModels).toHaveBeenCalledOnce();
  });

  it('uses the backend own catalog when the Model Hub cannot be read at all', async () => {
    const claudeModels = vi.fn().mockResolvedValue({ ok: true, models: ['native-a'] });
    const api = {
      readModelHubAgentCatalogForModelPicker: noHubCatalog(),
      claudeModels,
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'claude')).resolves.toMatchObject({ models: ['native-a'] });
    expect(claudeModels).toHaveBeenCalledOnce();
  });

  it('refuses a record that describes a different backend', async () => {
    const codexModels = vi.fn().mockResolvedValue({ ok: true, models: ['gpt-native'] });
    const api = {
      readModelHubAgentCatalogForModelPicker: vi
        .fn()
        .mockResolvedValue(hubAgent('claude', { catalog_models: [model('alpha')] })),
      codexModels,
    } as unknown as ApiContextType;

    await expect(fetchBackendModels(api, 'codex')).resolves.toMatchObject({ models: ['gpt-native'] });
  });
});

describe('loadBackendModelsWithRefresh', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('delivers the immediate snapshot and silently refetches after refresh', async () => {
    vi.useFakeTimers();
    const codexModels = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        models: ['gpt-old'],
        catalog_refresh_pending: true,
      })
      .mockResolvedValueOnce({
        ok: true,
        models: ['gpt-old', 'gpt-new'],
        catalog_refresh_pending: false,
      });
    const api = {
      readModelHubAgentCatalogForModelPicker: noHubCatalog(),
      codexModels,
    } as unknown as ApiContextType;
    const snapshots: string[][] = [];

    const cancel = loadBackendModelsWithRefresh(api, 'codex', (result) => {
      snapshots.push(result.models);
    });

    await vi.advanceTimersByTimeAsync(0);
    expect(snapshots).toEqual([['gpt-old']]);

    await vi.advanceTimersByTimeAsync(3_500);
    expect(snapshots).toEqual([['gpt-old'], ['gpt-old', 'gpt-new']]);
    expect(codexModels).toHaveBeenCalledTimes(2);

    cancel();
  });

  it('settles on the persisted catalog without polling for a refresh it cannot have', async () => {
    vi.useFakeTimers();
    const readModelHubAgentCatalogForModelPicker = vi
      .fn()
      .mockResolvedValue(hubAgent('codex', { catalog_models: [model('kept')] }));
    const api = { readModelHubAgentCatalogForModelPicker } as unknown as ApiContextType;
    const snapshots: string[][] = [];

    const cancel = loadBackendModelsWithRefresh(api, 'codex', (result) => {
      snapshots.push(result.models);
    });

    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(3_500);

    // A stored list has no remote refresh pending behind it, so the loader has
    // nothing to wait for and asks once.
    expect(snapshots).toEqual([['kept']]);
    expect(readModelHubAgentCatalogForModelPicker).toHaveBeenCalledOnce();

    cancel();
  });
});
