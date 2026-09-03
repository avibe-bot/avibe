import { describe, expect, it, vi } from 'vitest';

import {
  applyBackendCatalogIntent,
  applyModelsDevMatch,
  backendCatalogIntent,
  backendCatalogIntentApplied,
  blankBackendModel,
  candidateBackendModel,
  catalogModelIds,
  catalogModels,
  backendModelId,
  MODELS_DEV_FIELDS,
  pickerGroups,
  readBackendCatalogBaseline,
  retireModelsDevMatch,
  sameBackendModel,
} from './backendCatalog';
import { inferProvider, type StandardVendors } from './menus/identifiers';
import type {
  AgentBackend,
  AgentSupply,
  BackendModel,
  BackendModelCandidates,
  ModelCandidate,
  ModelsDevMatch,
} from './types';

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
  /** These cases are about the fields a match fills, so they use a backend
   *  whose ids carry no provider segment; the id rule has its own describe. */
  const NO_VENDORS: ReadonlySet<string> = new Set();

  it('names the row after the model that was picked and fills the rest from it', () => {
    const draft = model('half-typed-anthro', { locked: true, routeable: false });

    const filled = applyModelsDevMatch(draft, match, 'models_dev', 'codex', NO_VENDORS);

    // Choosing a suggestion is choosing a model, not decorating one, so the row
    // carries that model's own id — the one a backend accepts, not the
    // models.dev catalog key.
    expect(filled.id).toBe(match.model_id);
    expect(filled.id).not.toBe(match.models_dev_id);
    // Asserted as the whole row rather than field by field: every field is
    // either answered by the match or left as the draft had it, so a field the
    // mirror gains fails here instead of quietly arriving unfilled.
    expect(filled).toEqual({
      ...draft,
      id: match.model_id,
      origin: 'models_dev',
      models_dev_id: match.models_dev_id,
      display_name: match.display_name,
      context_window: match.context_window,
      max_output_tokens: match.max_output_tokens,
      input_modalities: match.input_modalities,
      output_modalities: match.output_modalities,
      supports_tools: match.supports_tools,
      supports_reasoning: match.supports_reasoning,
      reasoning_efforts: match.reasoning_efforts,
    });
    // The server's own projections are not the match's to state.
    expect(filled.locked).toBe(true);
    expect(filled.routeable).toBe(false);
  });

  it('takes the origin from the caller so a re-fill never rewrites how the row was created', () => {
    expect(applyModelsDevMatch(model('m', { origin: 'manual' }), match, 'manual', 'codex', NO_VENDORS).origin).toBe('manual');
    expect(applyModelsDevMatch(blankBackendModel(), match, 'models_dev', 'codex', NO_VENDORS).origin).toBe('models_dev');
  });

  it('copies the match lists instead of aliasing them', () => {
    const filled = applyModelsDevMatch(model('m'), match, 'models_dev', 'codex', NO_VENDORS);
    filled.reasoning_efforts.push('mutated');

    expect(match.reasoning_efforts).toEqual(['low', 'high']);
  });
});

/**
 * Retiring a fill, asserted over every field the fill owns.
 *
 * The rule has two halves and they are decided per field, so a test that named
 * fields would be a list of the ones somebody thought of. `MODELS_DEV_FIELDS` is
 * the fixture instead: every field is walked, and the values the two halves are
 * told apart by are derived from each field's shape rather than written down —
 * so a field the mirror gains is covered here without this file being edited,
 * and a field added to `applyModelsDevMatch` alone fails the first case.
 */
describe('retireModelsDevMatch', () => {
  const NO_VENDORS: ReadonlySet<string> = new Set();
  /** A match differing from the blank row in every field it fills, so 「the fill
   *  set this」 is never indistinguishable from 「it was already blank」. */
  const sentinel: ModelsDevMatch = {
    ...match,
    model_id: 'sentinel',
    display_name: 'Sentinel',
    context_window: 1,
    max_output_tokens: 2,
    input_modalities: ['image'],
    output_modalities: ['audio'],
    supports_tools: null,
    supports_reasoning: true,
    reasoning_efforts: ['sentinel'],
  };
  const fill = () => applyModelsDevMatch(blankBackendModel(), sentinel, 'models_dev', 'codex', NO_VENDORS);

  const same = (left: unknown, right: unknown): boolean =>
    Array.isArray(left) && Array.isArray(right)
      ? left.length === right.length && left.every((value, index) => value === right[index])
      : left === right;

  const changedFrom = (before: BackendModel, after: BackendModel): Set<string> =>
    new Set(Object.keys(after).filter((key) => !same(
      before[key as keyof BackendModel],
      after[key as keyof BackendModel],
    )));

  /** A value distinct from both the fill's and the blank row's, whatever shape
   *  the field has — otherwise 「the user typed this」 could read as 「this was
   *  retired」. Chosen by shape, never by field name. */
  const typedByUser = (filled: unknown, blank: unknown): unknown => {
    if (Array.isArray(filled)) return [...filled, 'text'];
    const choices: unknown[] = typeof filled === 'number' || typeof blank === 'number'
      ? [7, 8]
      : typeof filled === 'string' || typeof blank === 'string'
        ? ['mine', 'yours']
        : [true, false, null];
    return choices.find((choice) => choice !== filled && choice !== blank);
  };

  it('owns exactly the fields the fill writes', () => {
    // Both directions at once: the constant matches the applier, and the
    // sentinel really does move every one of them off the blank floor.
    expect(changedFrom(blankBackendModel(), fill())).toEqual(new Set([...MODELS_DEV_FIELDS, 'id']));
  });

  it('takes back every field the fill still owns, and only those', () => {
    const filled = fill();

    const retired = retireModelsDevMatch(filled, filled, 'typed-instead');

    expect(retired).toEqual({ ...blankBackendModel(), id: 'typed-instead' });
  });

  for (const field of MODELS_DEV_FIELDS) {
    it(`keeps ${field} when the user typed it themselves`, () => {
      const blank = blankBackendModel();
      const filled = fill();
      const mine = typedByUser(filled[field], blank[field]);
      const edited: BackendModel = { ...filled, ...{ [field]: mine } };

      const retired = retireModelsDevMatch(edited, filled, 'typed-instead');

      // Theirs survives; the rest of the fill goes, so the row is never left
      // describing a model whose id it no longer carries.
      expect(retired[field]).toEqual(mine);
      expect(retired).toEqual({ ...blank, id: 'typed-instead', ...{ [field]: mine } });
    });
  }
});

/**
 * The id rule, asserted over every path that produces one.
 *
 * Stated this way on purpose. The rule was first written for the 「use what I
 * typed」 escape, and a second producer — choosing a models.dev suggestion —
 * then shipped with a bare id OpenCode rejects, because nothing said the rule
 * belonged to all of them. So the producers are the fixture: a third one is
 * added to this list and inherits the whole property, or it is not added and
 * this file is where that shows.
 */
describe('the id every producer mints', () => {
  const PRODUCERS: readonly { what: string; mint: (typed: string, backend: AgentBackend, vendors: StandardVendors) => string }[] = [
    {
      what: 'the 「use what I typed」 escape',
      mint: (typed, backend, vendors) => backendModelId(typed, backend, vendors),
    },
    {
      what: 'a chosen models.dev suggestion',
      mint: (typed, backend, vendors) => applyModelsDevMatch(
        blankBackendModel(),
        { ...match, model_id: typed, provider_id: 'zhipuai' },
        'models_dev',
        backend,
        vendors,
      ).id,
    },
  ];

  const VENDORS: StandardVendors = new Set(['anthropic', 'zhipuai']);
  const TYPED = ['glm-5.2', 'zhipuai/glm-5.2', 'anthropic/claude', 'nosuchvendor/glm-5.2', 'custom/glm-5.2', 'a/b/c'];

  for (const { what, mint } of PRODUCERS) {
    it(`gives OpenCode an admissible provider segment from ${what}`, () => {
      for (const typed of TYPED) {
        const id = mint(typed, 'opencode', VENDORS);
        // Parsed as the backend parses it: an id with no slash has no provider
        // segment at all, which fails the check below for the same reason a
        // wrong one does.
        const slash = id.indexOf('/');
        const provider = slash > 0 ? id.slice(0, slash) : '';
        // The property, not a table of expected strings: whatever the segment
        // is, the backend's own normalization has to agree it is already that
        // segment — which is exactly the check `set_opencode_menu` applies.
        expect(inferProvider(provider, VENDORS), `${what} produced ${id} from ${typed}`).toBe(provider);
      }
    });

    it(`leaves a backend without provider segments alone, from ${what}`, () => {
      for (const typed of TYPED) {
        expect(mint(typed, 'codex', VENDORS)).toBe(typed);
      }
    });
  }

  it('takes the offering provider when it is standard and falls back to custom when it is not', () => {
    const from = (providerId: string, vendors: StandardVendors) => applyModelsDevMatch(
      blankBackendModel(),
      { ...match, model_id: 'glm-5.2', provider_id: providerId },
      'models_dev',
      'opencode',
      vendors,
    ).id;

    expect(from('zhipuai', VENDORS)).toBe('zhipuai/glm-5.2');
    // Not `zhipuai/…`: an unrecognized vendor is one provider called `custom`
    // there, so naming it would produce an id the menu rejects.
    expect(from('zhipuai', new Set())).toBe('custom/glm-5.2');
  });

  it('has nothing to prefix an empty id with', () => {
    expect(backendModelId('', 'opencode', VENDORS)).toBe('');
  });

  it('gives the escape a custom provider, because a query names no vendor', () => {
    expect(backendModelId('glm-5.2', 'opencode', VENDORS)).toBe('custom/glm-5.2');
  });
});

describe('candidateBackendModel', () => {
  const candidate: ModelCandidate = {
    id: 'glm-5.2',
    display_name: 'GLM 5.2',
    reasoning_efforts: ['low', 'high'],
    suppliers: [{ source_id: 'src_relay0001', source_name: 'relay.example', model_id: 'glm-5.2-air' }],
    origin: 'provider',
  };

  it('copies what the server proposed and leaves every other field at the blank floor', () => {
    const drafted = candidateBackendModel(candidate);

    // `PUT` stores the request literally, so a context window nobody stated
    // would persist as if the user had. Whole-row equality is what says so: a
    // value the proposal grows is either answered here or still the floor.
    expect(drafted).toEqual({
      ...blankBackendModel(),
      id: candidate.id,
      display_name: candidate.display_name,
      origin: candidate.origin,
      reasoning_efforts: candidate.reasoning_efforts,
    });
    // The suppliers the picker displayed travel as the write's
    // `expected_suppliers`; a catalog row names no Source at all.
    expect(Object.keys(drafted)).not.toContain('suppliers');
  });

  it('copies the proposed efforts instead of aliasing them', () => {
    candidateBackendModel(candidate).reasoning_efforts.push('mutated');

    expect(candidate.reasoning_efforts).toEqual(['low', 'high']);
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

describe('pickerGroups', () => {
  const offered = (id: string, overrides: Partial<ModelCandidate> = {}): ModelCandidate => ({
    id,
    display_name: null,
    reasoning_efforts: [],
    suppliers: [],
    origin: 'provider',
    ...overrides,
  });

  const read = (groups: Partial<BackendModelCandidates> = {}): BackendModelCandidates => ({
    builtin: groups.builtin ?? [],
    providers: groups.providers ?? [],
    in_list: groups.in_list ?? [],
  });

  it('files every id exactly once, whatever the response repeats', () => {
    // The property the picker's own copy depends on: a count beside a group
    // header, and an id under one of them. Neither survives a response whose
    // groups overlap, and only the client can hold the line — the read is three
    // lists, not a map.
    const groups = pickerGroups(read({
      builtin: [offered('shared'), offered('shared'), offered('gpt-6', { origin: 'builtin' })],
      providers: [offered('shared'), offered('glm-5.2')],
      in_list: [offered('shared'), offered('kimi-k3'), offered('gpt-6', { origin: 'builtin' })],
    }), new Set(['kimi-k3']));

    const filed = [...groups.builtin, ...groups.providers, ...groups.listed].map((entry) => entry.id);
    expect(filed).toHaveLength(new Set(filed).size);
    expect(new Set(filed)).toEqual(new Set(['shared', 'gpt-6', 'glm-5.2', 'kimi-k3']));
  });

  it('takes membership from the draft rather than from the saved list', () => {
    // The read projects the SAVED menu; the list behind the dialog is a draft.
    // Reading `in_list` literally would call a row the user just removed
    // 「already in the list」 and offer a row they just added as if it were new.
    const response = read({
      builtin: [offered('gpt-6', { origin: 'builtin' })],
      providers: [offered('glm-5.2')],
      in_list: [offered('kimi-k3', { origin: 'builtin' })],
    });

    const groups = pickerGroups(response, new Set(['glm-5.2']));

    // Added in the draft, so it is listed even though the server has not seen it.
    expect(groups.listed.map((entry) => entry.id)).toEqual(['glm-5.2']);
    // Removed in the draft, so it returns to the group that will serve it once
    // that removal saves.
    expect(groups.builtin.map((entry) => entry.id)).toEqual(['kimi-k3', 'gpt-6']);
    expect(groups.providers).toEqual([]);
  });

  it('leaves a removed custom row to the action that can recreate it', () => {
    // A hand-written row belongs to no group: no backend ships it and no
    // provider supplies it. Filing it under one would name a supplier that does
    // not exist, so `Add custom model…` is its way back instead.
    const groups = pickerGroups(read({ in_list: [offered('internal/house', { origin: 'manual' })] }), new Set());

    expect(groups).toEqual({ builtin: [], providers: [], listed: [] });
  });
});
