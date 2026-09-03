import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';

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
  draftRowFor,
  draftWithId,
  echoableRefusal,
  heldRowFor,
  offeredCandidates,
  opencodeMenuIdentity,
  orderWithRestored,
  MODELS_DEV_FIELDS,
  pickerGroups,
  readBackendCatalogBaseline,
  retireModelsDevMatch,
  sameBackendModel,
  samePlanContents,
} from './backendCatalog';
import { inferProvider, type StandardVendors } from './menus/identifiers';
import type {
  AgentSupply,
  BackendModel,
  BackendModelCandidates,
  ModelCandidate,
  ModelCandidateSupplier,
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
 * The id rule, asserted where ids are written instead of at each writer.
 *
 * This started as a table of producers, and the table is what kept failing. The
 * rule was written for the 「use what I typed」 escape; choosing a models.dev
 * suggestion then shipped with a bare id OpenCode rejects; the picker's seed did
 * it again. A list of producers cannot state this property, because the producer
 * that is missing from the list is exactly the one nobody checked — and the list
 * reads complete either way.
 *
 * So the property belongs to `draftWithId`, the one write that gives a draft row
 * its id, and it is stated over arbitrary input rather than over known callers:
 * whatever this function is handed, what it writes is admissible. A producer
 * added later inherits it by calling the only function that can write the field,
 * and the boundary test below is what keeps 「the only」 true.
 */
describe('the id chokepoint', () => {
  const VENDORS: StandardVendors = new Set(['anthropic', 'zhipuai']);
  /** Not a list of cases that must pass — a spread of shapes the field can hold:
   *  no vendor, a standard one, an unknown one, the escape hatch, extra
   *  separators, and separators in the positions that parse to nothing. */
  const HANDED = [
    'glm-5.2',
    'zhipuai/glm-5.2',
    'anthropic/claude',
    'nosuchvendor/glm-5.2',
    'custom/glm-5.2',
    'a/b/c',
    '/leading',
    'trailing/',
  ];
  /** Who offered the model, when anyone did: the picker and the escape name no
   *  vendor, a models.dev match names its provider, standard or not. */
  const OFFERED_BY = ['', 'zhipuai', 'nosuchvendor'];

  /** Admissible as the backend decides it, not as this test decides it. This
   *  used to ask the vendor list — whether OpenCode's own normalization agreed
   *  the segment was that segment — and that was the defect, stated as the
   *  property: `canonical_opencode_menu_identity` admits any segment matching
   *  its grammar and never consults `standard_vendors`, so the test called
   *  `nosuchvendor/glm-5.2` inadmissible and locked in a rewrite the server had
   *  no objection to. The one mirror of that rule is the authority here too. */
  const admissible = (id: string): boolean => opencodeMenuIdentity(id, 'opencode');
  /** Each bucket asserted below, and asserted to be inhabited: a filter that
   *  matches nothing states its property vacuously forever. */
  const bucket = (of: (id: string) => boolean): string[] => {
    const ids = HANDED.filter(of);
    expect(ids.length).toBeGreaterThan(0);
    return ids;
  };

  it('round-trips an identity that already names a provider, standard or not', () => {
    for (const handed of bucket(admissible)) {
      for (const vendor of OFFERED_BY) {
        // Byte-identical, `nosuchvendor/…` included. An id the user gave is the
        // public model they asked for, and the server splits on the first
        // separator, so `custom/nosuchvendor/glm-5.2` would be accepted as a
        // different model with nothing downstream to notice the substitution.
        expect(draftWithId(blankBackendModel(), handed, 'opencode', VENDORS, vendor).id).toBe(handed);
      }
    }
  });

  it('gives an id that names no provider exactly one, from whoever offered it', () => {
    for (const handed of bucket((id) => !id.includes('/'))) {
      for (const vendor of OFFERED_BY) {
        const written = draftWithId(blankBackendModel(), handed, 'opencode', VENDORS, vendor).id;
        // The vendor list's one job: which segment a SOURCE's vendor maps to,
        // byte-matching `opencode_model_id(source.vendor, model.id)`.
        expect(written).toBe(`${inferProvider(vendor, VENDORS)}/${handed}`);
        expect(admissible(written), `${handed} from ${vendor || 'nobody'} was written as ${written}`).toBe(true);
      }
    }
  });

  it('leaves a malformed identity malformed instead of completing it into another one', () => {
    for (const handed of bucket((id) => id.includes('/') && !admissible(id))) {
      for (const vendor of OFFERED_BY) {
        const written = draftWithId(blankBackendModel(), handed, 'opencode', VENDORS, vendor).id;
        // The prefix is for an id that names no provider, and these name a
        // broken one. `custom//leading` and `custom/trailing/` are both
        // admissible to the server and neither is what was typed, so a typo
        // would be saved as a row instead of refused at the field — which is
        // where admissibility is asked, of this write's own output.
        expect(written).toBe(handed);
        expect(admissible(written)).toBe(false);
      }
    }
  });

  it('is settled by one pass, so resolving an already-resolved id changes nothing', () => {
    for (const handed of HANDED) {
      for (const vendor of OFFERED_BY) {
        const once = draftWithId(blankBackendModel(), handed, 'opencode', VENDORS, vendor);
        // Why a producer may route a value through here twice — an editor that
        // resolves at commit what the escape already resolved — without the
        // prefix accumulating.
        expect(draftWithId(once, once.id, 'opencode', VENDORS, vendor).id).toBe(once.id);
      }
    }
  });

  it('has nothing to prefix the blank floor with', () => {
    // The one id that is not admissible and must still pass: a draft opens with
    // no id at all, and 「required」 is the editor's answer to that, not a vendor.
    expect(draftWithId(blankBackendModel(), '', 'opencode', VENDORS).id).toBe('');
  });

  it('leaves a backend without provider segments alone', () => {
    for (const handed of HANDED) {
      expect(draftWithId(blankBackendModel(), handed, 'codex', VENDORS).id).toBe(handed);
    }
  });

  it('writes the id and nothing else', () => {
    const row = model('kept', { display_name: 'Kept', context_window: 200_000, origin: 'models_dev' });
    expect(draftWithId(row, 'glm-5.2', 'opencode', VENDORS)).toEqual({ ...row, id: 'custom/glm-5.2' });
  });

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

  it('gives an id that names no vendor a custom provider', () => {
    expect(backendModelId('glm-5.2', 'opencode', VENDORS)).toBe('custom/glm-5.2');
  });
});

describe('the OpenCode identity rule', () => {
  // One clause of `canonical_opencode_menu_identity` (config/v2_config.py) each,
  // named by what the server checks, so a rule that moves there is findable
  // here. The server is still the authority; this only decides early enough for
  // the id field to say what is wrong before the `PUT` rejects the whole list.
  it.each([
    // `not separator`: nothing splits the halves.
    ['glm-5.2', false],
    // `not model_id`: the hole this closes. Any provider prefix is taken as
    // given by the chokepoint, which hands `openai/` straight back — and
    // `custom/` is the same hole behind the fallback prefix it adds itself.
    ['openai/', false],
    ['custom/', false],
    // `not provider`: a leading separator names no provider.
    ['/glm-5.2', false],
    // The provider segment's own shape: lowercase alphanumeric runs, joined by
    // single `.`, `_` or `-`.
    ['OpenAI/glm-5.2', false],
    ['-openai/glm-5.2', false],
    ['open_ai/glm-5.2', true],
    ['relay.example/glm-5.2', true],
    // `identifier != identifier.strip()`, then `model_id != model_id.strip()`.
    [' openai/glm-5.2', false],
    ['openai/glm-5.2 ', false],
    ['openai/ glm-5.2', false],
    // Split on the FIRST separator, so a reseller keeps its own: this is
    // provider `openrouter` serving the model `anthropic/claude-x`.
    ['openrouter/anthropic/claude-x', true],
    ['custom/glm-5.2-air', true],
  ])('mirrors the server rule for %s', (id, admissible) => {
    expect(opencodeMenuIdentity(id, 'opencode')).toBe(admissible);
  });

  it('judges nothing for a backend whose ids have no segments to satisfy', () => {
    // claude and codex ids are flat, and their admission rules stay the
    // server's alone: a copy here would be a second authority over admission
    // that drifts silently (Known-by-design 22).
    for (const backend of ['claude', 'codex'] as const) {
      for (const id of ['openai/', 'claude-sonnet-4-5', 'anything at all']) {
        expect(opencodeMenuIdentity(id, backend)).toBe(true);
      }
    }
  });
});

/**
 * What makes 「the only write」 true.
 *
 * The property above is about `draftWithId`; it says nothing about a producer
 * that sets `id` itself. That is the defect that shipped twice, so it is checked
 * mechanically rather than by review: outside the module that owns the rule,
 * `backendModelId` may be read for display but never assigned to an `id`. A
 * producer that reaches for the resolver directly is one that is about to write
 * a draft field without the write that carries the rule.
 */
const MODELS_DIR = join(process.cwd(), 'src/components/settings/models');
const OWNER = 'backendCatalog.ts';

/**
 * Every Model Hub module that ships, by path relative to the tree's root.
 *
 * Recursive, and tests excluded. Recursive because a producer one folder down is
 * exactly the one a flat read would miss, and a boundary test that can be
 * escaped by moving a file is a boundary in name only.
 */
const shippedModules = (): { name: string; source: string }[] => {
  const walk = (dir: string): string[] => readdirSync(dir, { withFileTypes: true }).flatMap((entry) => (
    entry.isDirectory() ? walk(join(dir, entry.name)) : [join(dir, entry.name)]
  ));
  return walk(MODELS_DIR)
    .filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path))
    .map((path) => ({ name: relative(MODELS_DIR, path), source: readFileSync(path, 'utf8') }));
};

/** Whoever calls this, outside the module that owns the rule, is who breaks it. */
const callersOutsideOwner = (call: RegExp) => shippedModules()
  .filter((module) => module.name !== OWNER && call.test(module.source))
  .map((module) => module.name);

describe('the id chokepoint boundary', () => {
  /** `id:` or `id =` fed from the resolver, however the call is spelled or
   *  wrapped — the assignment is the part that matters, not the formatting. */
  const ASSIGNS_FROM_RESOLVER = /\bid\s*[:=]\s*[^;,\n]*\bbackendModelId\s*\(/;

  it('lets no module outside the resolver’s own write the id from it', () => {
    expect(callersOutsideOwner(ASSIGNS_FROM_RESOLVER)).toEqual([]);
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

  it('copies what the server proposed and states nothing else at all', () => {
    const drafted = candidateBackendModel(candidate);

    // `PUT` stores the request literally, so a context window nobody stated
    // would persist as if the user had. Whole-row equality is what says so: a
    // value the proposal grows is either answered here or still unstated.
    //
    // Written out rather than spread from `blankBackendModel()`, because THAT
    // is the row under test. Spreading the editor's floor here would assert
    // that a picked row inherits the editor's defaults, which is the defect: a
    // new manual default would flow into every picked row and this test would
    // approve it. A literal makes the row's own claims the subject, so a field
    // added to `BackendModel` has to be decided here before it can ship.
    expect(drafted).toEqual({
      id: candidate.id,
      display_name: candidate.display_name,
      origin: candidate.origin,
      reasoning_efforts: candidate.reasoning_efforts,
      models_dev_id: null,
      context_window: null,
      max_output_tokens: null,
      // No editor opened, so there was nobody to show a default to: the row
      // asserts no capability and no modality. `null` is not `false` — the
      // projection omits the capability and the backend's own default stands.
      input_modalities: [],
      output_modalities: [],
      supports_tools: null,
      supports_reasoning: null,
      locked: false,
      routeable: true,
    });
    // The editor's floor is a different row and stays that way: these four are
    // exactly what it adds, and none of them may reach a pick.
    expect(blankBackendModel()).toMatchObject({
      input_modalities: ['text'],
      output_modalities: ['text'],
      supports_tools: true,
      supports_reasoning: false,
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

/**
 * Which row an id names, stated over where a row can be, not over who asks.
 *
 * The defect this closes reached the user through two different doors — the
 * picker re-adding a removed model and the editor being seeded with its id — so
 * the property is about the stores, and every door inherits it by asking. The
 * proposal is held constant throughout: what decides the answer is where a row
 * already exists, never what the server said about it.
 */
describe('the row chokepoint', () => {
  const proposal: ModelCandidate = {
    id: 'glm-5.2',
    display_name: 'GLM 5.2',
    reasoning_efforts: ['low'],
    suppliers: [{ source_id: 'src_relay0001', source_name: 'relay.example', model_id: 'glm-5.2-air' }],
    origin: 'provider',
  };

  /** A row carrying what a proposal cannot restate: the fields the user stated
   *  themselves, which are exactly the ones the defect cleared. */
  const stated = (id: string) => model(id, {
    display_name: 'GLM 5.2 (mine)',
    context_window: 200_000,
    max_output_tokens: 64_000,
    input_modalities: ['text', 'image'],
    output_modalities: ['text'],
    supports_tools: true,
    models_dev_id: 'zhipuai/glm-5.2',
    origin: 'models_dev',
  });

  /** Every arrangement of the two stores, so 「wherever it exists」 is covered by
   *  construction rather than by the cases anyone thought to name. A store that
   *  holds an unrelated row is what says the answer comes from a match and not
   *  from a store being non-empty. */
  const HOLDING = [
    { where: 'the draft', held: [stated('glm-5.2')], saved: [] as BackendModel[] },
    { where: 'the baseline', held: [], saved: [stated('glm-5.2')] },
    { where: 'both', held: [stated('glm-5.2')], saved: [stated('glm-5.2')] },
    { where: 'the draft, beside other rows', held: [model('alpha'), stated('glm-5.2')], saved: [model('beta')] },
    { where: 'the baseline, beside other rows', held: [model('alpha')], saved: [model('beta'), stated('glm-5.2')] },
  ];

  it('answers with the row that already exists, wherever it exists', () => {
    for (const { where, held, saved } of HOLDING) {
      expect(heldRowFor(proposal.id, held, saved), where).toEqual(stated('glm-5.2'));
      expect(draftRowFor(proposal, held, saved), where).toEqual(stated('glm-5.2'));
    }
  });

  it('yields the baseline row when a removed saved model is re-added', () => {
    const saved = stated('glm-5.2');
    // 「Remove it, change my mind, re-add it」: the draft no longer holds the row,
    // the baseline still does, and `PUT`'s three-way merge reads any difference
    // as an edit — so anything short of deep equality persists as a clearing the
    // user never asked for.
    expect(draftRowFor(proposal, [], [saved])).toEqual(saved);
  });

  it('builds from the proposal only when neither store holds the id', () => {
    expect(heldRowFor(proposal.id, [model('alpha')], [model('beta')])).toBeNull();
    expect(draftRowFor(proposal, [model('alpha')], [model('beta')])).toEqual(candidateBackendModel(proposal));
  });

  it('has nothing to hand back for the id a blank draft opens with', () => {
    // The editor's seed is the query the user typed, and it is empty when they
    // asked for a custom row without typing one. Nothing may match it, or that
    // door would open on whatever row the list happens to hold.
    expect(heldRowFor('', [stated('glm-5.2')], [stated('alpha')])).toBeNull();
  });
});

/**
 * What makes 「every door asks」 true.
 *
 * The property above is about the stores; it says nothing about a producer that
 * builds a row from a candidate itself, which is the defect that shipped three
 * times. So it is checked mechanically: outside the module that owns the rule,
 * nothing calls the builder at all — reaching for it is reaching past the only
 * function that can find the row the user already has.
 */
describe('the row chokepoint boundary', () => {
  const BUILDS_FROM_A_CANDIDATE = /\bcandidateBackendModel\s*\(/;

  it('lets no module outside the chokepoint’s own build a row from a candidate', () => {
    expect(callersOutsideOwner(BUILDS_FROM_A_CANDIDATE)).toEqual([]);
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

describe('orderWithRestored', () => {
  const BASELINE = ['a', 'b', 'c', 'd'];

  const without = (...ids: string[]) => BASELINE.filter((id) => !ids.includes(id));

  it('puts a cancelled removal back where the baseline had it, wherever that was', () => {
    // The property, stated for every position rather than for the one that
    // happens to be interesting: undoing a removal restores the baseline
    // exactly. A row that came back at the end would answer 「are you sure?」
    // with a reordered catalog the user never asked for — and one that reports
    // itself as edited afterwards can never be saved back to what it was.
    for (const id of BASELINE) {
      expect(orderWithRestored(without(id), BASELINE, new Set([id])), id).toEqual(BASELINE);
    }
    // Including all of them at once, in any combination: the restored rows keep
    // their baseline sequence among themselves, so neighbours cannot swap.
    expect(orderWithRestored(without('b', 'c'), BASELINE, new Set(['b', 'c']))).toEqual(BASELINE);
    expect(orderWithRestored([], BASELINE, new Set(BASELINE))).toEqual(BASELINE);
  });

  it('leaves every other edit alone', () => {
    // A refusal answers one removal; it says nothing about a reorder or an
    // addition the user also made, and those are still theirs afterwards. So
    // the baseline decides positions and is never replayed as the order.
    expect(orderWithRestored(['d', 'a', 'c'], BASELINE, new Set(['b'])))
      .toEqual(['d', 'a', 'b', 'c']);
    expect(orderWithRestored(['a', 'c', 'd', 'new'], BASELINE, new Set(['b'])))
      .toEqual(['a', 'b', 'c', 'd', 'new']);
    expect(orderWithRestored(['a', 'b', 'c', 'd'], BASELINE, new Set())).toEqual(BASELINE);
  });

  it('restores an id whose baseline neighbours are all gone', () => {
    // Nothing to anchor to is still an answer: the front, because a row with no
    // surviving predecessor had none in the baseline either.
    expect(orderWithRestored(['d'], BASELINE, new Set(['b']))).toEqual(['b', 'd']);
    expect(orderWithRestored(['new'], BASELINE, new Set(['c']))).toEqual(['c', 'new']);
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
      builtin: [offered('shared'), offered('shared'), offered('gpt-6')],
      providers: [offered('shared'), offered('glm-5.2')],
      in_list: [
        offered('shared', { group_if_removed: 'providers' }),
        offered('kimi-k3'),
        offered('gpt-6', { group_if_removed: 'builtin' }),
      ],
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
      builtin: [offered('gpt-6')],
      providers: [offered('glm-5.2')],
      in_list: [offered('kimi-k3', { group_if_removed: 'builtin' })],
    });

    const groups = pickerGroups(response, new Set(['glm-5.2']));

    // Added in the draft, so it is listed even though the server has not seen it.
    expect(groups.listed.map((entry) => entry.id)).toEqual(['glm-5.2']);
    // Removed in the draft, so it returns to the group that will serve it once
    // that removal saves.
    expect(groups.builtin.map((entry) => entry.id)).toEqual(['kimi-k3', 'gpt-6']);
    expect(groups.providers).toEqual([]);
  });

  it('files a draft-removed row by what supplies it now, never by the path that created it', () => {
    // Why `group_if_removed` exists at all (C4): `origin` records the creation
    // path and nothing else (C2), so a row added through a provider whose Source
    // was deleted months ago still reads `provider`. Filing by that would offer
    // it under 「From your providers」 and name a supplier that no longer exists.
    // So the group is a function of the server's own answer alone — and a row
    // that answer places nowhere is absent from the picker, with `Add custom
    // model…` as its way back, which costs the user far less than a row filed
    // under a supplier nothing can serve.
    const ORIGINS: ModelCandidate['origin'][] = ['builtin', 'models_dev', 'manual', 'provider'];
    const SUPPLIED: ModelCandidateSupplier[] = [{ source_id: 'src', source_name: 'Src', model_id: 'upstream' }];

    /** One row per shape the read can answer `group_if_removed` in, absence
     *  included, each paired with the group it must reach. Seeded over the
     *  field's closed domain rather than listed as cases: a value the server
     *  gains is one row here, and every origin below then covers it. Every row
     *  pairs the answer with a `suppliers` that would disagree with it, so a
     *  result can only have come from the answer itself — and the last two say
     *  an absent answer reads exactly as `null`, whatever else the row carries,
     *  because there is nothing else the client is entitled to read it from. */
    const FACTS: {
      what: string;
      supply: Partial<ModelCandidate>;
      group: 'builtin' | 'providers' | null;
    }[] = [
      { what: 'the server names the built-in snapshot', supply: { group_if_removed: 'builtin', suppliers: SUPPLIED }, group: 'builtin' },
      { what: 'the server names the providers', supply: { group_if_removed: 'providers', suppliers: [] }, group: 'providers' },
      { what: 'the server names nowhere', supply: { group_if_removed: null, suppliers: SUPPLIED }, group: null },
      { what: 'no answer, and a provider supplies it', supply: { suppliers: SUPPLIED }, group: null },
      { what: 'no answer, and nothing supplies it', supply: { suppliers: [] }, group: null },
    ];

    /** The one group a candidate reaches, and proof there is only one. */
    const groupOf = (candidate: ModelCandidate): 'builtin' | 'providers' | 'listed' | null => {
      const groups = pickerGroups(read({ in_list: [candidate] }), new Set());
      const reached = (['builtin', 'providers', 'listed'] as const).filter((name) => groups[name].length > 0);
      expect(reached.length).toBeLessThan(2);
      return reached[0] ?? null;
    };

    for (const fact of FACTS) {
      const reached = ORIGINS.map((origin) => groupOf(offered('kimi-k3', { ...fact.supply, origin })));
      // One group for one answer, whatever origin arrived with it…
      expect(new Set(reached).size, fact.what).toBe(1);
      // …and it is the group that answer names.
      expect(reached[0], fact.what).toBe(fact.group);
    }
  });
});

describe('offeredCandidates', () => {
  const candidate = (id: string, overrides: Partial<ModelCandidate> = {}): ModelCandidate => ({
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

  const supplier: ModelCandidateSupplier = { source_id: 'src_a', source_name: 'Primary relay', model_id: 'up' };

  it('offers every pickable id, whatever its suppliers say', () => {
    // The property: being offered and having suppliers are two different facts,
    // and only the first one this answers. A candidate the server serves with
    // nothing behind it yet is a row the user may add — its route starts empty —
    // so a supplier list can never be the reason an id is missing from here.
    const rows = [
      candidate('supplied', { suppliers: [supplier] }),
      candidate('unsupplied'),
      candidate('many', { suppliers: [supplier, { ...supplier, source_id: 'src_b' }] }),
    ];

    for (const group of ['builtin', 'providers'] as const) {
      const offered = offeredCandidates(read({ [group]: rows }));
      expect([...offered.keys()], group).toEqual(rows.map((row) => row.id));
      expect([...offered.values()], group).toEqual(rows);
    }
  });

  it('answers for the ids the picker would offer, and files each of them once', () => {
    // One answer, not two: the withdrawal decision here and the rows the picker
    // puts in front of the user are the same question, so 「offered」 is the
    // pickable groups in group order with the same first-wins dedupe — a
    // response that repeats an id across them cannot make it two offers.
    const arrival = read({
      builtin: [candidate('shared'), candidate('shared', { display_name: 'again' }), candidate('gpt-6')],
      providers: [candidate('shared'), candidate('glm-5.2')],
    });
    const groups = pickerGroups(arrival, new Set());

    expect([...offeredCandidates(arrival).keys()])
      .toEqual([...groups.builtin, ...groups.providers].map((entry) => entry.id));
  });

  it('never offers an id the saved menu already holds', () => {
    // `in_list` is not an offer, whatever it carries: it reports what the saved
    // menu holds, and a row already in the list is not one this dialog can offer
    // to add. `group_if_removed` says where such a row would RE-enter if it were
    // removed, which is a question about a removal nobody has made.
    const arrival = read({
      in_list: [candidate('kimi-k3', { group_if_removed: 'providers' }), candidate('gpt-6')],
      providers: [candidate('glm-5.2')],
    });

    expect([...offeredCandidates(arrival).keys()]).toEqual(['glm-5.2']);
  });

  it('drops an id the response left blank', () => {
    expect([...offeredCandidates(read({ providers: [candidate(''), candidate('glm-5.2')] })).keys()])
      .toEqual(['glm-5.2']);
  });
});

describe('samePlanContents', () => {
  const hop = (position: number) => ({
    source_id: 'src', model_id: 'kimi-k3', backend: 'claude', menu_model: 'kimi-k3', position,
  });
  const gap = (agents: string[]) => ({ backend: 'claude', model_id: 'kimi-k3', agents });

  /** Every ordering of a list. */
  const orderings = <T>(list: readonly T[]): T[][] => (
    list.length <= 1
      ? [[...list]]
      : list.flatMap((head, index) => orderings([...list.slice(0, index), ...list.slice(index + 1)])
        .map((rest) => [head, ...rest]))
  );

  it('reads any two orderings of the same consequences as one plan', () => {
    // The decision it serves: 「is the server's refusal the one the user already
    // confirmed?」. The preview follows the order the user clicked and the
    // refusal follows the server's own walk of the baseline, so an order that
    // differs between them is not a disagreement about what would happen — and
    // asking the same question twice for it is the bug.
    const plan = [hop(1), hop(2), gap(['reviewer'])];
    const every = orderings(plan);
    expect(every).toHaveLength(6);
    expect(every.filter((ordering) => !samePlanContents(plan, ordering))).toEqual([]);
  });

  it('reads consequences that are not the same as a different plan', () => {
    // What makes the automatic retry safe: a server whose answer moved between
    // the question and the retry has to ask it again.
    const plan = [hop(1), hop(2)];
    const moved = [
      [],                       // nothing left
      [hop(1)],                 // one fewer
      [hop(1), hop(2), hop(3)], // one more
      [hop(1), hop(1)],         // one replaced by a copy of the other
      [hop(1), hop(9)],         // one altered
    ];
    expect(moved.filter((other) => samePlanContents(plan, other))).toEqual([]);
    // Including inside an element, where no story explains a reordering: one
    // side produced that list, so a different order there is a real change.
    expect(samePlanContents([gap(['a', 'b'])], [gap(['b', 'a'])])).toBe(false);
  });

  it('reads no difference into how the same element happens to be written', () => {
    // Both halves are JSON off a wire, and neither side controls the other's
    // key order or how it spells a field it has nothing to say about.
    expect(samePlanContents(
      [{ source_id: 'src', model_id: 'kimi-k3', position: 1 }],
      [{ position: 1, model_id: 'kimi-k3', source_id: 'src', menu_model: undefined }],
    )).toBe(true);
  });
});

describe('echoableRefusal', () => {
  const BASELINE = [model('alpha'), model('beta'), model('gamma')];
  const REQUESTED = [model('alpha')];
  const stored = (owed: readonly string[]) => ({
    baseline: BASELINE,
    models: REQUESTED,
    owed: new Set(owed),
  });

  it('permits an echo only where the refusal was accepted AND still describes this write', () => {
    // Two independent facts decide it, so what is stated here is their product
    // rather than a list of cases: whether every held-back removal has been
    // answered against the server's own plan, and whether the write is still
    // the one the server refused. `force` asserts both at once — 「the user saw
    // this consequence, and it is the consequence of what I am sending」 — so
    // the answer is their conjunction, and each dimension is free to grow
    // without the expectations being rewritten.
    const ACCEPTANCE = [
      { what: 'every question answered', owed: [], accepted: true },
      { what: 'one still owed', owed: ['beta'], accepted: false },
      { what: 'every one still owed', owed: ['beta', 'gamma'], accepted: false },
    ];
    const SUBJECT = [
      { what: 'the write it refused', baseline: BASELINE, requested: REQUESTED, same: true },
      { what: 'a draft that removed more since', baseline: BASELINE, requested: [], same: false },
      { what: 'a draft that put a row back', baseline: BASELINE, requested: BASELINE, same: false },
      { what: 'a draft that added a row', baseline: BASELINE, requested: [...REQUESTED, model('delta')], same: false },
      { what: 'the same ids, one row edited', baseline: BASELINE, requested: [model('alpha', { display_name: 'Alpha' })], same: false },
      { what: 'a newer server catalog', baseline: [...BASELINE, model('delta')], requested: REQUESTED, same: false },
    ];
    const cells = ACCEPTANCE.flatMap((acceptance) => SUBJECT.map((subject) => ({
      what: `${acceptance.what} · ${subject.what}`,
      echoable: echoableRefusal(stored(acceptance.owed), subject.baseline, subject.requested),
      accepted: acceptance.accepted,
      same: subject.same,
    })));
    expect(cells.filter((cell) => cell.echoable !== (cell.accepted && cell.same))).toEqual([]);
    // And the product is the whole point: neither fact on its own permits an
    // echo, so exactly one cell of the matrix does.
    expect(cells.filter((cell) => cell.echoable).map((cell) => cell.what))
      .toEqual(['every question answered · the write it refused']);
  });

  it('permits nothing when there is no refusal to echo', () => {
    // The state a fresh dialog saves from, and the one it returns to after a
    // reopen: there is no server plan, so there is nothing to force.
    expect(echoableRefusal(null, BASELINE, REQUESTED)).toBe(false);
  });
});
