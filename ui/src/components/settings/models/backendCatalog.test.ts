import { describe, expect, it, vi } from 'vitest';

import {
  applyBackendCatalogIntent,
  applyModelsDevMatch,
  backendCatalogIntent,
  backendCatalogIntentApplied,
  blankBackendModel,
  catalogModelIds,
  catalogModels,
  readBackendCatalogBaseline,
  sameBackendModel,
} from './backendCatalog';
import type { AgentSupply, BackendModel, ModelsDevMatch } from './types';

const model = (id: string, overrides: Partial<BackendModel> = {}): BackendModel => ({
  ...blankBackendModel(),
  id,
  ...overrides,
});

const agent: AgentSupply = {
  backend: 'claude',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'fixed',
  sources: { order: [], eligibility: [] },
  builtin_models: ['legacy-a', 'legacy-b'],
  routes: {},
  menu: null,
};

const match: ModelsDevMatch = {
  provider_id: 'anthropic',
  provider_name: 'Anthropic',
  model_id: 'claude-sonnet-4-5',
  models_dev_id: 'anthropic/claude-sonnet-4-5',
  display_name: 'Claude Sonnet 4.5',
  context_window: 200000,
  max_output_tokens: 64000,
  input_modalities: ['text', 'image', 'pdf'],
  output_modalities: ['text'],
  supports_tools: true,
  supports_reasoning: true,
  reasoning_efforts: ['low', 'high'],
};

describe('catalogModelIds', () => {
  it('enumerates routeable rows in catalog order', () => {
    const catalogued: AgentSupply = {
      ...agent,
      catalog_models: [
        model('second'),
        model('default', { locked: true, routeable: false }),
        model('first'),
      ],
    };

    expect(catalogModelIds(catalogued)).toEqual(['second', 'first']);
  });

  it('keeps a locked row routeable when the server says it names a route key', () => {
    const catalogued: AgentSupply = { ...agent, catalog_models: [model('pinned', { locked: true })] };

    expect(catalogModelIds(catalogued)).toEqual(['pinned']);
  });

  it('falls back to the legacy projection while the server predates the catalog', () => {
    expect(catalogModels(agent)).toBeNull();
    expect(catalogModelIds(agent)).toEqual(['legacy-a', 'legacy-b']);
    expect(catalogModelIds({ ...agent, menu_kind: 'open', builtin_models: null, menu: { view: 'featured', checked: ['x'] } }))
      .toEqual(['x']);
  });

  it('prefers an empty catalog over the legacy projection once the server sends one', () => {
    expect(catalogModelIds({ ...agent, catalog_models: [] })).toEqual([]);
  });
});

describe('applyModelsDevMatch', () => {
  it('fills metadata without ever renaming the row', () => {
    const filled = applyModelsDevMatch(model('anthropic/claude-sonnet-4-5-20250929'), match, 'models_dev');

    expect(filled.id).toBe('anthropic/claude-sonnet-4-5-20250929');
    expect(filled.models_dev_id).toBe('anthropic/claude-sonnet-4-5');
    expect(filled.display_name).toBe('Claude Sonnet 4.5');
    expect(filled.context_window).toBe(200000);
    expect(filled.max_output_tokens).toBe(64000);
    expect(filled.input_modalities).toEqual(['text', 'image', 'pdf']);
    expect(filled.reasoning_efforts).toEqual(['low', 'high']);
  });

  it('takes the origin from the caller so a re-fill never rewrites how the row was created', () => {
    expect(applyModelsDevMatch(model('m', { origin: 'manual' }), match, 'manual').origin).toBe('manual');
    expect(applyModelsDevMatch(blankBackendModel(), match, 'models_dev').origin).toBe('models_dev');
  });

  it('copies the match lists instead of aliasing them', () => {
    const filled = applyModelsDevMatch(model('m'), match, 'models_dev');
    filled.reasoning_efforts.push('mutated');

    expect(match.reasoning_efforts).toEqual(['low', 'high']);
  });
});

describe('sameBackendModel', () => {
  it('ignores the server-derived projections', () => {
    expect(sameBackendModel(model('m'), model('m', { locked: true, routeable: false }))).toBe(true);
  });

  it('separates an unstated capability from a stated no', () => {
    // Treating them as equal would swallow the write that answers the question.
    expect(sameBackendModel(model('m', { supports_tools: null }), model('m', { supports_tools: false }))).toBe(false);
    expect(sameBackendModel(model('m', { supports_tools: null }), model('m', { supports_tools: null }))).toBe(true);
  });

  it('sees every field the user owns, including list order', () => {
    expect(sameBackendModel(model('m'), model('m', { display_name: 'M' }))).toBe(false);
    expect(sameBackendModel(model('m'), model('m', { context_window: 1 }))).toBe(false);
    expect(sameBackendModel(
      model('m', { reasoning_efforts: ['low', 'high'] }),
      model('m', { reasoning_efforts: ['high', 'low'] }),
    )).toBe(false);
  });
});

describe('backendCatalogIntent', () => {
  const baseline = [model('a'), model('b'), model('c')];

  it('records removals, upserts and the desired order as ids', () => {
    const intent = backendCatalogIntent(baseline, [model('c'), model('a', { display_name: 'A' }), model('d')]);

    expect([...intent.removed]).toEqual(['b']);
    expect(intent.upserts.map((entry) => entry.id)).toEqual(['a', 'd']);
    expect(intent.order).toEqual(['c', 'a', 'd']);
  });

  it('treats a pure reorder as no upsert at all', () => {
    const intent = backendCatalogIntent(baseline, [model('c'), model('b'), model('a')]);

    expect(intent.upserts).toEqual([]);
    expect(intent.order).toEqual(['c', 'b', 'a']);
  });
});

describe('applyBackendCatalogIntent', () => {
  it('replays edits onto a newer catalog and keeps a concurrent addition visible', () => {
    const intent = backendCatalogIntent([model('a'), model('b')], [model('b'), model('a', { display_name: 'A' })]);
    const rebased = applyBackendCatalogIntent([model('a'), model('b'), model('fresh')], intent);

    expect(rebased.map((entry) => entry.id)).toEqual(['b', 'a', 'fresh']);
    expect(rebased[1].display_name).toBe('A');
  });

  it('never removes or edits a locked row', () => {
    const current = [model('default', { locked: true, routeable: false }), model('a')];
    const intent = backendCatalogIntent(current, [model('default', { display_name: 'renamed' })]);
    const rebased = applyBackendCatalogIntent(current, intent);

    expect(rebased.map((entry) => entry.id)).toEqual(['default']);
    expect(rebased[0].display_name).toBeNull();
    expect(rebased[0].locked).toBe(true);
  });

  it('keeps the server projections when an edited row lands on a newer catalog', () => {
    const intent = backendCatalogIntent([model('a')], [model('a', { display_name: 'A', routeable: true })]);
    const rebased = applyBackendCatalogIntent([model('a', { routeable: false })], intent);

    expect(rebased[0].display_name).toBe('A');
    expect(rebased[0].routeable).toBe(false);
  });

  it('drops an ordered id the server no longer has', () => {
    const intent = backendCatalogIntent([model('a'), model('b')], [model('b'), model('a')]);

    expect(applyBackendCatalogIntent([model('b')], intent).map((entry) => entry.id)).toEqual(['b']);
  });
});

describe('backendCatalogIntentApplied', () => {
  const intent = backendCatalogIntent([model('a'), model('b')], [model('b'), model('a', { display_name: 'A' })]);

  it('accepts a server catalog that already carries the removals and upserts', () => {
    expect(backendCatalogIntentApplied([model('b'), model('a', { display_name: 'A', locked: true })], intent)).toBe(true);
  });

  it('rejects a catalog that kept a removed row or lost an upsert', () => {
    const removal = backendCatalogIntent([model('a'), model('b')], [model('a')]);

    expect(backendCatalogIntentApplied([model('a'), model('b')], removal)).toBe(false);
    expect(backendCatalogIntentApplied([model('b'), model('a')], intent)).toBe(false);
  });

  it('rejects the old order after an inconclusive reorder-only save', () => {
    const reorder = backendCatalogIntent(
      [model('a'), model('b')],
      [model('b'), model('a')],
    );

    expect(backendCatalogIntentApplied([model('a'), model('b')], reorder)).toBe(false);
    expect(backendCatalogIntentApplied([model('b'), model('a')], reorder)).toBe(true);
  });
});

describe('readBackendCatalogBaseline', () => {
  it('reports the catalog and the agent it came from', async () => {
    const catalogued = { ...agent, catalog_models: [model('a')] };
    const api = { getAgentSources: vi.fn().mockResolvedValue(catalogued) };

    await expect(readBackendCatalogBaseline(api, 'claude')).resolves.toEqual({
      agent: catalogued,
      models: [model('a')],
    });
  });

  it('refuses a read that answered for another backend', async () => {
    // The one thing a baseline cannot survive: describing someone else's list.
    await expect(readBackendCatalogBaseline({ getAgentSources: vi.fn().mockResolvedValue(agent) }, 'codex'))
      .rejects.toThrow('Backend model catalog is unavailable');
  });

  it('refuses a direct-mode catalog that the runtime will not project', async () => {
    const direct = { ...agent, mode: 'direct' as const, routes: null, catalog_models: [model('a')] };

    await expect(readBackendCatalogBaseline({ getAgentSources: vi.fn().mockResolvedValue(direct) }, 'claude'))
      .rejects.toThrow('Backend model catalog is unavailable');
  });
});
