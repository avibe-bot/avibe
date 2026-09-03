// The backend model catalog: what a backend Agent's menu holds, and how an edit
// to it settles.
//
// One concept for all three backends. Their runtime adapters still differ —
// Claude Code reads the Gateway's Anthropic projection, Codex gets a written
// `model_catalog_json`, OpenCode gets a provider overlay — but those are derived
// artifacts. The catalog is the state, and this module is the only place that
// reads it, orders it, or turns a draft into a write.
//
// It deliberately knows nothing about Sources or Routes. A catalog row names a
// model the Agent may select; which supplier serves it stays with the Route, and
// mixing the two here is what the contract's 「no Source id, no upstream model
// id, no priority, no fallback」 rule exists to prevent.
import { buildIdentifier, inferProvider, type StandardVendors } from './menus/identifiers';
import type { ModelsApi } from './modelsApi';
import type {
  AgentBackend,
  AgentSupply,
  BackendModel,
  BackendModelCandidates,
  BackendModelOrigin,
  ModelCandidate,
  ModelsDevMatch,
} from './types';

/**
 * The model list the server says this backend exposes, derived from the
 * pre-catalog projections.
 *
 * Kept for the rolling-upgrade window only: a server that predates
 * `catalog_models` still answers with `builtin_models`/`menu`, and an Agent card
 * that rendered nothing there would report 「no models」 about a backend that has
 * them. The extras are the same rule the server's own migration applies — a
 * configured Route never becomes invisible just because it postdates the menu.
 */
export function listedModelIds(agent: AgentSupply): string[] {
  const primary = agent.menu_kind === 'fixed' ? agent.builtin_models ?? [] : agent.menu?.checked ?? [];
  const extras = [
    ...(agent.selected_model_id ? [agent.selected_model_id] : []),
    ...(agent.model_supply ?? []).map((model) => model.model_id),
    ...Object.keys(agent.routes ?? {}),
  ];
  const seen = new Set<string>();
  return [...primary, ...extras].filter((modelId) => {
    if (!modelId || seen.has(modelId)) return false;
    seen.add(modelId);
    return true;
  });
}

/** The canonical catalog, or null when this server predates it. Null is the
 *  whole compatibility signal: nothing downstream invents rows to stand in for
 *  a payload the server never sent. */
export const catalogModels = (agent: AgentSupply): BackendModel[] | null => agent.catalog_models ?? null;

export function routeableCatalogModelIds(catalog: readonly BackendModel[]): string[] {
  const seen = new Set<string>();
  return catalog
    .filter((model) => model.routeable && model.id && !seen.has(model.id) && seen.add(model.id))
    .map((model) => model.id);
}

/**
 * The model ids an Agent card enumerates, in catalog order.
 *
 * `routeable` is the filter, not `locked`: Claude Code's `Default` is a backend
 * selector that never names a Route key, so a Route row for it would open an
 * editor with nothing to configure. Pre-catalog servers fall back to the legacy
 * projection — OpenCode to its saved menu (dormant routes left behind by an
 * earlier selection are not menu entries), everything else to `listedModelIds`.
 */
export function catalogModelIds(agent: AgentSupply): string[] {
  const catalog = catalogModels(agent);
  if (catalog) {
    return routeableCatalogModelIds(catalog);
  }
  if (agent.menu_kind !== 'open') return listedModelIds(agent);
  return [...new Set(agent.menu?.checked ?? [])].filter(Boolean);
}

/** A new row's starting point: text in, text out, tools on — the floor for any
 *  model a coding-agent backend can actually drive. Reasoning stays off because
 *  an empty `reasoning_efforts` is a decision (「omit the effort parameter」),
 *  not a gap the UI may pre-fill on the user's behalf. */
export const blankBackendModel = (): BackendModel => ({
  id: '',
  display_name: null,
  origin: 'manual',
  models_dev_id: null,
  context_window: null,
  max_output_tokens: null,
  input_modalities: ['text'],
  output_modalities: ['text'],
  supports_tools: true,
  supports_reasoning: false,
  reasoning_efforts: [],
  locked: false,
  routeable: true,
});

/**
 * A picked candidate, poured into a draft row.
 *
 * Copies exactly the three values the server proposed (C2) and leaves every
 * other field at the blank floor. That asymmetry is the contract: the proposal
 * covers what the product already knows about the model — its label and the
 * efforts its suppliers accept — and the rest stays empty until the user fills
 * it, because `PUT` stores the request literally and an invented context window
 * would persist as if the user had stated it.
 *
 * `origin` comes from the candidate rather than from the group the row was
 * rendered in: the server names the creation path, and reading it back off the
 * group would be this client re-deriving something it was told. The cast is
 * transitional — `provider` joins `BackendModelOrigin` on the head that carries
 * the version bump, and it goes away when this branch rebases onto it.
 */
export const candidateBackendModel = (candidate: ModelCandidate): BackendModel => ({
  ...blankBackendModel(),
  id: candidate.id,
  display_name: candidate.display_name,
  origin: candidate.origin as BackendModelOrigin,
  reasoning_efforts: [...candidate.reasoning_efforts],
});

export type PickerGroups = {
  builtin: ModelCandidate[];
  providers: ModelCandidate[];
  /** Search-only, and never pickable. */
  listed: ModelCandidate[];
};

export const EMPTY_PICKER_GROUPS: PickerGroups = { builtin: [], providers: [], listed: [] };

/**
 * The three groups the picker renders, reconciled against the draft the catalog
 * dialog holds.
 *
 * The read projects the SAVED menu, and the list behind that dialog is a draft,
 * so `in_list` on its own would call a row the user just removed 「already in the
 * list」 and offer a row the user just added as though it were new. The draft
 * decides membership; the server still decides what a candidate is, which group
 * serves it, and which suppliers it has.
 *
 * A draft-removed row re-enters the group its own `origin` names — the same
 * group the server will serve it from once that removal saves. A custom row has
 * no such group, and `Add custom model…` is its way back, so it is absent rather
 * than filed under a provider that does not supply it.
 *
 * Filing each id once, in group order, is what makes 「every candidate appears
 * exactly once」 a property of the code rather than of the response.
 */
export const pickerGroups = (
  candidates: BackendModelCandidates,
  listedIds: ReadonlySet<string>,
): PickerGroups => {
  const groups: PickerGroups = { builtin: [], providers: [], listed: [] };
  const filed = new Set<string>();
  const file = (candidate: ModelCandidate, group: 'builtin' | 'providers' | null) => {
    if (!candidate.id || filed.has(candidate.id)) return;
    filed.add(candidate.id);
    if (listedIds.has(candidate.id)) {
      groups.listed.push(candidate);
      return;
    }
    const target = group
      ?? (candidate.origin === 'builtin' ? 'builtin' : candidate.origin === 'provider' ? 'providers' : null);
    if (target) groups[target].push(candidate);
  };
  for (const candidate of candidates.in_list) file(candidate, null);
  for (const candidate of candidates.builtin) file(candidate, 'builtin');
  for (const candidate of candidates.providers) file(candidate, 'providers');
  return groups;
};

/**
 * An id the backend will accept, whoever proposed it.
 *
 * Every id this UI *produces* comes through here — the models.dev suggestion the
 * user chose and the 「use what I typed」 escape alike. That is the whole point of
 * one function: an id the backend rejects is discovered at save time, long after
 * the row was built, so the two places that mint one cannot each carry their own
 * rule. An id the user types character by character is not produced here; it is
 * theirs, and the id field validates it rather than rewriting it underneath them.
 *
 * OpenCode identifiers are `provider/model`, and the provider segment has to be
 * one the backend recognizes: an unrecognized vendor is `custom` there, never
 * the vendor's own name (`menus/identifiers`). So an id that already carries an
 * admissible provider is taken as given, and anything else is prefixed with the
 * normalization of `vendor` — the proposing provider when there is one, and
 * nothing when there is not, which `inferProvider` answers with `custom` for the
 * same reason the backend does. Every other backend takes the id verbatim,
 * because its ids have no segment to satisfy.
 *
 * Admissibility is asked of `inferProvider` rather than of the vendor list
 * directly, so an id that already says `custom/…` passes for exactly the reason
 * the backend accepts it.
 */
export const backendModelId = (
  id: string,
  backend: AgentBackend,
  standardVendors: StandardVendors,
  vendor = '',
): string => {
  if (backend !== 'opencode' || id === '') return id;
  const slash = id.indexOf('/');
  const provider = slash > 0 ? id.slice(0, slash) : '';
  if (provider !== '' && inferProvider(provider, standardVendors) === provider) return id;
  return buildIdentifier(vendor, id, standardVendors);
};

/**
 * A models.dev match, poured into a draft — the id included.
 *
 * Choosing a suggestion is choosing a model, not decorating one: the row it
 * creates is the model that was picked, so it carries that model's own id
 * (`model_id`, the id a backend accepts — not the catalog key `models_dev_id`),
 * resolved through `backendModelId` against the provider that offered it. The
 * user typed a search, and every filled field including the id stays editable
 * afterwards. Keeping what was typed is the 「use what I typed」 escape, which
 * never reaches this function — but reaches the same resolver.
 *
 * `origin` is passed in rather than derived because the schema defines it as how
 * the row was FIRST created — an existing row keeps its own answer no matter how
 * often it is later re-filled.
 */
export const applyModelsDevMatch = (
  draft: BackendModel,
  match: ModelsDevMatch,
  origin: BackendModelOrigin,
  backend: AgentBackend,
  standardVendors: StandardVendors,
): BackendModel => ({
  ...draft,
  id: backendModelId(match.model_id, backend, standardVendors, match.provider_id),
  display_name: match.display_name,
  origin,
  models_dev_id: match.models_dev_id,
  context_window: match.context_window,
  max_output_tokens: match.max_output_tokens,
  input_modalities: [...match.input_modalities],
  output_modalities: [...match.output_modalities],
  supports_tools: match.supports_tools,
  supports_reasoning: match.supports_reasoning,
  reasoning_efforts: [...match.reasoning_efforts],
});

const sameList = (left: readonly unknown[], right: readonly unknown[]): boolean =>
  left.length === right.length && left.every((value, index) => value === right[index]);

const sameValue = (left: unknown, right: unknown): boolean =>
  Array.isArray(left) && Array.isArray(right) ? sameList(left, right) : left === right;

/** The fields a models.dev answer decides. Stated once, beside the function that
 *  writes them, and checked against it by a test that applies a match differing
 *  from blank in every field — so a field added to `applyModelsDevMatch` is
 *  retired by the same change that fills it, or the test says so. */
export const MODELS_DEV_FIELDS = [
  'display_name',
  'origin',
  'models_dev_id',
  'context_window',
  'max_output_tokens',
  'input_modalities',
  'output_modalities',
  'supports_tools',
  'supports_reasoning',
  'reasoning_efforts',
] as const satisfies readonly (keyof BackendModel)[];

/**
 * Un-apply a models.dev answer, because the id it answered about is being
 * retyped.
 *
 * `filled` is the draft exactly as that answer left it, which is what makes the
 * two halves separable. A field still equal to it is models.dev's statement
 * about a model the user is no longer naming — keeping one model's context
 * window under another model's id would save a fact nobody ever made — so it
 * goes back to the blank floor. A field that differs is the user's own typing
 * since, and correcting an id is not a decision to retype the rest of the form.
 *
 * `id` is set from the caller either way: it is the field being edited, never a
 * field being retired.
 */
export const retireModelsDevMatch = (
  draft: BackendModel,
  filled: BackendModel,
  id: string,
): BackendModel => {
  const blank = blankBackendModel();
  const next: BackendModel = { ...draft, id };
  for (const field of MODELS_DEV_FIELDS) {
    if (sameValue(draft[field], filled[field])) Object.assign(next, { [field]: blank[field] });
  }
  return next;
};

/** Equality over the fields a user owns. `locked` and `routeable` are server
 *  projections, so a server that recomputed them has not made the row a user
 *  edit — comparing them would turn every reconnect into a spurious write. */
export const sameBackendModel = (left: BackendModel, right: BackendModel): boolean =>
  left.id === right.id
  && left.display_name === right.display_name
  && left.origin === right.origin
  && left.models_dev_id === right.models_dev_id
  && left.context_window === right.context_window
  && left.max_output_tokens === right.max_output_tokens
  && sameList(left.input_modalities, right.input_modalities)
  && sameList(left.output_modalities, right.output_modalities)
  && left.supports_tools === right.supports_tools
  && left.supports_reasoning === right.supports_reasoning
  && sameList(left.reasoning_efforts, right.reasoning_efforts);

export const sameCatalog = (left: readonly BackendModel[], right: readonly BackendModel[]): boolean =>
  left.length === right.length && left.every((model, index) => sameBackendModel(model, right[index]));

export type BackendCatalogIntent = {
  removed: ReadonlySet<string>;
  /** Rows this caller added or changed, keyed by id. */
  upserts: BackendModel[];
  /** The caller's complete desired order. */
  order: string[];
};

/** What the user actually did, expressed as ids rather than a whole list — so a
 *  rebase onto a newer server catalog can apply exactly those changes and leave
 *  a concurrent editor's untouched row alone. */
export const backendCatalogIntent = (
  baseline: readonly BackendModel[],
  draft: readonly BackendModel[],
): BackendCatalogIntent => {
  const before = new Map(baseline.map((model) => [model.id, model]));
  const drafted = new Set(draft.map((model) => model.id));
  return {
    removed: new Set(baseline.filter((model) => !drafted.has(model.id)).map((model) => model.id)),
    upserts: draft.filter((model) => {
      const previous = before.get(model.id);
      return !previous || !sameBackendModel(previous, model);
    }),
    order: draft.map((model) => model.id),
  };
};

/**
 * The user's edits, replayed onto a newer catalog.
 *
 * A locked row survives every branch: it cannot be removed and it cannot be
 * edited, so a draft that appears to do either is describing a row the server
 * changed underneath it, not a user decision to honor. Rows the caller never
 * named keep their server order at the end, which is what makes a concurrent
 * addition visible instead of silently dropped.
 */
export const applyBackendCatalogIntent = (
  current: readonly BackendModel[],
  intent: BackendCatalogIntent,
): BackendModel[] => {
  const kept = current.filter((model) => model.locked || !intent.removed.has(model.id));
  const byId = new Map(kept.map((model) => [model.id, model]));
  for (const upsert of intent.upserts) {
    const existing = byId.get(upsert.id);
    if (existing?.locked) continue;
    byId.set(
      upsert.id,
      existing ? { ...upsert, locked: existing.locked, routeable: existing.routeable } : upsert,
    );
  }
  const ordered: BackendModel[] = [];
  const placed = new Set<string>();
  for (const id of intent.order) {
    const model = byId.get(id);
    if (!model || placed.has(id)) continue;
    ordered.push(model);
    placed.add(id);
  }
  for (const model of kept) {
    if (placed.has(model.id)) continue;
    ordered.push(byId.get(model.id) ?? model);
    placed.add(model.id);
  }
  return ordered;
};

/** Whether a catalog the server now holds already carries this intent — the
 *  only honest answer to an inconclusive PUT, which may have committed. */
export const backendCatalogIntentApplied = (
  current: readonly BackendModel[],
  intent: BackendCatalogIntent,
): boolean => {
  const byId = new Map(current.map((model) => [model.id, model]));
  const intendedIds = new Set(intent.order);
  const intendedOrder = intent.order.filter((id) => byId.has(id));
  const observedOrder = current
    .map((model) => model.id)
    .filter((id) => intendedIds.has(id));
  return [...intent.removed].every((id) => !byId.has(id))
    && intent.upserts.every((upsert) => {
      const landed = byId.get(upsert.id);
      return Boolean(landed) && sameBackendModel(landed as BackendModel, upsert);
    })
    && sameList(observedOrder, intendedOrder);
};

export type BackendCatalogBaseline = {
  agent: AgentSupply;
  /** null when this server predates `catalog_models`: the dialog then reads the
   *  legacy projection and refuses to write, rather than sending a baseline it
   *  assembled itself. */
  models: BackendModel[] | null;
};

/** The catalog read a mutation is allowed to be based on. Unlike the Source
 *  order, a catalog has no second projection to converge against: the list is
 *  the state, so one fresh per-backend read is the whole baseline.
 *
 *  The response must describe the requested backend in gateway mode. Direct
 *  backends use their native provider menu, so accepting that snapshot here
 *  would expose an editor whose saved rows are not projected into the runtime. */
export const readBackendCatalogBaseline = async (
  api: Pick<ModelsApi, 'getAgentSources'>,
  backend: AgentBackend,
): Promise<BackendCatalogBaseline> => {
  const agent = await api.getAgentSources(backend);
  if (agent.backend !== backend || agent.mode !== 'hub') {
    throw new Error('Backend model catalog is unavailable');
  }
  return { agent, models: catalogModels(agent) };
};
