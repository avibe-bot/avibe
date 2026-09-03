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
import { buildIdentifier, type StandardVendors } from './menus/identifiers';
import type { ModelsApi } from './modelsApi';
import type {
  AgentBackend,
  AgentSupply,
  BackendModel,
  BackendModelCandidates,
  BackendModelOrigin,
  ModelCandidate,
  ModelsDevMatch,
  RouteHop,
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

/**
 * A row that states nothing — the floor under every other floor here.
 *
 * It is the server's own default for a row it is told nothing about: no
 * modality, and no capability, where `null` is not `false` but 「the projection
 * omits this, so the backend's own default stands」.
 *
 * Every floor below is built from this one so that the direction of the default
 * is 「unstated unless stated」: a field added to `BackendModel` starts unstated
 * in every producer, and a value one of them wants has to be written down to
 * exist. Building the other way round is what put `supports_reasoning: false`
 * into rows nobody had opened — the runtime projection reads that as 「this model
 * does not reason」 and drops the very efforts the row was created with.
 */
const unstatedBackendModel = (): BackendModel => ({
  id: '',
  display_name: null,
  origin: 'manual',
  models_dev_id: null,
  context_window: null,
  max_output_tokens: null,
  input_modalities: [],
  output_modalities: [],
  supports_tools: null,
  supports_reasoning: null,
  reasoning_efforts: [],
  locked: false,
  routeable: true,
});

/** A row the user is about to WRITE BY HAND: text in, text out, tools on — the
 *  floor for any model a coding-agent backend can actually drive. These four are
 *  stated rather than unstated because the editor renders them before it saves
 *  anything, so they are on screen to be changed; a row created without the
 *  editor ever opening starts from `unstatedBackendModel` instead. Reasoning
 *  stays off because an empty `reasoning_efforts` is a decision (「omit the
 *  effort parameter」), not a gap the UI may pre-fill on the user's behalf. */
export const blankBackendModel = (): BackendModel => ({
  ...unstatedBackendModel(),
  input_modalities: ['text'],
  output_modalities: ['text'],
  supports_tools: true,
  supports_reasoning: false,
});

/**
 * A picked candidate, poured into a draft row.
 *
 * Copies exactly the three values the server proposed (C2) and leaves every
 * other field unstated. That asymmetry is the contract: the proposal covers what
 * the product already knows about the model — its label and the efforts its
 * suppliers accept — and the rest stays empty until the user fills it, because
 * `PUT` stores the request literally and an invented context window would
 * persist as if the user had stated it.
 *
 * Unstated, NOT the blank floor. The blank floor's `text`/`text`/tools-on/
 * reasoning-off belong to a row the editor is about to show, where they are on
 * screen to be corrected. This row is created by clicking a checkbox in the
 * picker: no editor opens, so anything the floor asserts is a claim the user
 * never saw and the server never made. `supports_reasoning: false` was the
 * expensive one — the runtime projection suppresses `reasoning_efforts`
 * entirely for a row that says it, so a reasoning-capable candidate picked from
 * the list arrived with its tiers and lost them on the way to the Route editor.
 *
 * `origin` comes from the candidate rather than from the group the row was
 * rendered in: the server names the creation path, and reading it back off the
 * group would be this client re-deriving something it was told.
 */
export const candidateBackendModel = (candidate: ModelCandidate): BackendModel => ({
  ...unstatedBackendModel(),
  id: candidate.id,
  display_name: candidate.display_name,
  origin: candidate.origin,
  reasoning_efforts: [...candidate.reasoning_efforts],
});

/**
 * Which row an id already names — the single place that decides it.
 *
 * A row the draft or the baseline holds is this user's own description of that
 * model, carrying the context limits, modalities, capabilities and
 * `models_dev_id` they stated. Anything that produces a row for an id has to ask
 * this before building one, because the answer is 「theirs」 far more often than
 * the producer can see: the draft is what is on screen, but the baseline still
 * holds a row the user removed a moment ago and may be re-adding right now.
 * Building over one is how 「remove it, change my mind, re-add it」 came to clear
 * those fields — the baseline still held the full row, so the PUT's three-way
 * merge read the fresh blank one as an edit that emptied them and persisted the
 * emptying.
 *
 * `draftWithId` above owns which id a produced row carries; this owns which row
 * an id names. Both are one function rather than a rule each producer remembers,
 * for the same reason: the producer that is missing from the list is exactly the
 * one nobody checked.
 */
export const heldRowFor = (
  id: string,
  held: readonly BackendModel[],
  saved: readonly BackendModel[],
): BackendModel | null => (
  held.find((model) => model.id === id)
  ?? saved.find((model) => model.id === id)
  ?? null
);

/**
 * The row a draft write lands for one candidate.
 *
 * A candidate is the server's proposal ABOUT a model (C2): a label, and the
 * efforts its suppliers accept. It is not a description of the row, so it is
 * only ever what a row is built from when no description exists yet.
 * `candidateBackendModel` is therefore this function's last branch and is
 * reached through nothing else outside this module, which the boundary test
 * beside it is what keeps true.
 */
export const draftRowFor = (
  candidate: ModelCandidate,
  held: readonly BackendModel[],
  saved: readonly BackendModel[],
): BackendModel => heldRowFor(candidate.id, held, saved) ?? candidateBackendModel(candidate);

/**
 * One picked candidate, and the projection it was picked against.
 *
 * This is the agreement `expected_suppliers` states (C1): the id the user chose
 * and the suppliers the picker displayed for it. One object, because two records
 * of one agreement can disagree — a set of picked ids beside a map of
 * expectations lets an id leave the set while its promise stays behind, and the
 * next save then sends a projection nobody agreed to. Here, dropping a pick
 * drops its promise with it, and there is no second place for a stale
 * expectation to survive in.
 */
export type ChosenCandidate = {
  candidate: ModelCandidate;
  /** The suppliers on screen, in the shape the write sends them. */
  expected_suppliers: RouteHop[];
};

/** A candidate paired with the suppliers displayed for it — the agreement, taken
 *  from exactly what the row shows. */
export const chosenCandidate = (candidate: ModelCandidate): ChosenCandidate => ({
  candidate,
  expected_suppliers: candidate.suppliers.map((supplier) => ({
    source_id: supplier.source_id,
    model_id: supplier.model_id,
  })),
});

/**
 * Every id this read OFFERS, and the candidate it offers for it.
 *
 * Withdrawal has one definition (C1) and this is its only evidence: an id is
 * withdrawn when the server stops offering it. A supplier list that came back
 * empty does not say so — `ModelCandidate.suppliers` is explicit that empty is
 * meaningful, an offered id nothing supplies yet, whose route starts empty — and
 * `origin` cannot say it either, because `origin` records the path a row was
 * created by (C2), not what supplies it now.
 *
 * Only the pickable groups count, in the order and with the first-wins dedupe
 * `pickerGroups` files them by, so 「offered」 means 「a row the picker would put
 * in front of the user」 rather than 「named somewhere in the response」. That
 * makes the answer here and the answer the picker reaches one answer. `in_list`
 * is not an offer: it names what the saved menu already holds.
 */
export const offeredCandidates = (
  candidates: BackendModelCandidates,
): Map<string, ModelCandidate> => {
  const offered = new Map<string, ModelCandidate>();
  for (const candidate of [...candidates.builtin, ...candidates.providers]) {
    if (!candidate.id || offered.has(candidate.id)) continue;
    offered.set(candidate.id, candidate);
  }
  return offered;
};

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
 * A draft-removed row re-enters wherever `group_if_removed` says, which is the
 * server's own reading of what supplies the id right now (C4) — `null` and
 * absent alike meaning nowhere. Nothing else on the row is consulted for it, and
 * `origin` least of all: `origin` records the path a row was created by (C2), so
 * reading current availability out of it offers a model whose provider was
 * deleted months ago under 「From your providers」. Nor could the client derive
 * the answer if it wanted to — `builtin` is served 「minus menu ids」, so a row
 * still in the menu is by construction absent from it.
 *
 * `Add custom model…` is the way back for anything that ends up nowhere, so
 * being absent costs the user nothing — while being present under a heading that
 * claims a supplier costs them a row nothing can serve.
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
    const target = group ?? candidate.group_if_removed ?? null;
    if (target) groups[target].push(candidate);
  };
  for (const candidate of candidates.in_list) file(candidate, null);
  for (const candidate of candidates.builtin) file(candidate, 'builtin');
  for (const candidate of candidates.providers) file(candidate, 'providers');
  return groups;
};

/** The provider segment `canonical_opencode_menu_identity` accepts, verbatim. */
const OPENCODE_PROVIDER_SEGMENT = /^[a-z0-9]+(?:[._-][a-z0-9]+)*$/;

/**
 * Whether an id is a whole OpenCode menu identity, or only looks like one.
 *
 * `canonical_opencode_menu_identity` is the authority and stays the authority:
 * this decides nothing the server does not, it just decides it early enough for
 * the id field to say what is wrong while the user is still typing. Without it
 * the dialog calls `openai/` valid, saves, and the `PUT` rejects the whole list
 * for a row the user was told was fine.
 *
 * Both halves or neither. A provider prefix proves only that the left half is
 * admissible — `openai/` has no right half at all, and `custom/` is the same
 * hole behind the fallback prefix this file adds itself. This is the only rule
 * that judges admissibility, which is why `backendModelId` can hand a malformed
 * identity straight back instead of inventing a well-formed one. Splitting on
 * the FIRST separator is what the backend does, so a reseller id keeps its own
 * slashes (`openrouter/anthropic/claude-…` is provider `openrouter`, model
 * `anthropic/claude-…`).
 *
 * The credential-material rule is deliberately not mirrored. It is a heuristic
 * over secret shapes that the server can revise whenever it learns a new one,
 * and a copy here would be a second opinion about what a secret looks like,
 * drifting silently in whichever direction it was last edited. A row it catches
 * is refused by the `PUT` with its own message.
 */
export const opencodeMenuIdentity = (id: string, backend: AgentBackend): boolean => {
  if (backend !== 'opencode') return true;
  if (id !== id.trim()) return false;
  const separator = id.indexOf('/');
  if (separator <= 0) return false;
  const model = id.slice(separator + 1);
  return OPENCODE_PROVIDER_SEGMENT.test(id.slice(0, separator))
    && model !== ''
    && model === model.trim();
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
 * OpenCode identifiers are `provider/model`, and what this function contributes
 * is the provider segment an id does not have. So the question it asks is
 * whether one is there at all, never whether the backend recognizes it:
 * `canonical_opencode_menu_identity` admits any segment matching its grammar,
 * `standard_vendors` membership included and not required, so `acme/model` is a
 * public identity the server accepts, and rewriting it to `custom/acme/model`
 * saves a different model than the user asked for — one the server accepts too,
 * because it splits on the first separator, so nothing downstream would notice.
 *
 * The vendor list answers a different question, and only that one: which
 * provider segment a SOURCE's vendor maps to, which has to byte-match
 * `opencode_model_id(source.vendor, model.id)` or the menu rejects the checked
 * value. That is what `vendor` is normalized through here — the proposing
 * provider when there is one, and nothing when there is not, which
 * `buildIdentifier` answers with `custom` for the same reason the backend does.
 * Every other backend takes the id verbatim, because its ids have no segment to
 * satisfy.
 *
 * An id that carries a provider segment is therefore taken as typed even when
 * the whole identity is malformed. `openai/` is not a bare model id, and
 * prefixing it would mint `custom/openai/` — a typo the server then accepts as
 * the model `openai/`. Admissibility is `opencodeMenuIdentity`'s question, asked
 * of this function's output at the field, and the only honest answer to a broken
 * identity is to refuse it rather than to complete it into a different one.
 */
export const backendModelId = (
  id: string,
  backend: AgentBackend,
  standardVendors: StandardVendors,
  vendor = '',
): string => {
  if (backend !== 'opencode' || id === '') return id;
  if (id.includes('/')) return id;
  return buildIdentifier(vendor, id, standardVendors);
};

/**
 * The one write that gives a draft row its id.
 *
 * Every id this UI produces lands through here — a models.dev fill, the picker's
 * 「add as a custom model」 seed, the 「use what I typed」 escape, and the row the
 * editor finally commits. One chokepoint rather than a rule each producer
 * remembers: a producer that skipped the resolver used to be a defect nobody
 * could see until the backend refused the saved id, and the answer to 「which
 * producers apply it?」 has to be 「there is no other way in」, not a list that a
 * later producer falls off.
 *
 * The id field's own keystrokes are the one thing that does not come through
 * here, and deliberately: while the user is typing, the value is theirs to
 * finish. What they typed is resolved once, when `commit` turns it into a row —
 * so the exemption cannot outlive the dialog.
 */
export const draftWithId = (
  draft: BackendModel,
  id: string,
  backend: AgentBackend,
  standardVendors: StandardVendors,
  vendor = '',
): BackendModel => ({ ...draft, id: backendModelId(id, backend, standardVendors, vendor) });

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
): BackendModel => draftWithId(
  {
    ...draft,
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
  },
  match.model_id,
  backend,
  standardVendors,
  match.provider_id,
);

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

/** A value written so that two equal values always read the same: object keys in
 *  one fixed order, absent and undefined fields spelled the same way. */
const canonicalJson = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const fields = Object.entries(value as Record<string, unknown>)
      .filter(([, field]) => field !== undefined)
      .sort(([left], [right]) => (left < right ? -1 : 1));
    return `{${fields.map(([key, field]) => `${JSON.stringify(key)}:${canonicalJson(field)}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
};

/**
 * Whether two halves of a guard plan name the same consequences, in whatever
 * order each of them happens to list them.
 *
 * It exists for one decision and produces nothing that is sent: it answers
 * 「is the server's refusal the one the user already confirmed?」, so a retry
 * that would ask the same question twice can be automatic instead. The arrays
 * the client echoes with `force` are always the server's own, verbatim and in
 * the server's order (C3) — which is exactly why comparing as a set is safe
 * here. Both orders are legitimate: the client's preview follows the order the
 * user clicked, the server's follows its own walk of the baseline, and neither
 * is a disagreement about what would happen.
 *
 * Only that outer list is read as a set. A list nested inside one element — a
 * gap's `agents`, say — has no such story: one side produced it, so an order
 * that differs there is a real change and the user is asked again. Strictness
 * costs a question; laxity would skip one.
 */
export const samePlanContents = (left: readonly unknown[], right: readonly unknown[]): boolean =>
  left.length === right.length
  && sameList(left.map(canonicalJson).sort(), right.map(canonicalJson).sort());

/**
 * Whether a stored guard refusal may still be echoed back with `force`.
 *
 * `force` is the client saying 「the user has been shown this consequence and
 * still wants it」, and two independent things have to be true before it can
 * say that honestly. `owed` empty is the acceptance: every removal the server
 * refused has been put to the user and confirmed. The two catalogs are the
 * subject: the refusal answers *this* save — same starting point, same request
 * — and not a later, different one.
 *
 * Neither implies the other, so neither alone is enough. A refusal the user
 * never answered must not be forced however exactly its catalogs match, and an
 * accepted one must not be carried onto a save it was never about. Either way
 * the fallback is the same and is not a failure: the save goes out unforced and
 * the server asks again.
 */
export const echoableRefusal = (
  refusal: {
    baseline: readonly BackendModel[];
    models: readonly BackendModel[];
    owed: ReadonlySet<string>;
  } | null,
  baseline: readonly BackendModel[],
  requested: readonly BackendModel[],
): boolean => refusal !== null
  && refusal.owed.size === 0
  && sameCatalog(refusal.baseline, baseline)
  && sameCatalog(refusal.models, requested);

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
 * An order with cancelled removals put back where they were.
 *
 * A removal the server refuses was never a decision, so undoing it may not cost
 * the row its place: the requested order is the list with the row already gone,
 * and appending it back would answer 「are you sure?」 with a reordered catalog
 * the user never asked for. The baseline is what says where it belongs — each
 * restored id goes back after its nearest baseline predecessor that is still on
 * screen, or at the front when it had none — so a removal that is cancelled
 * leaves the draft byte-identical to the baseline, and the position is recovered
 * from the two lists rather than from a snapshot somebody has to remember to
 * take.
 *
 * Only the restored ids move. Anything else the user did to the order is a
 * separate edit the refusal said nothing about, and an independent reorder is
 * still theirs afterwards — which is why the baseline is read for positions
 * rather than replayed as the order. Walking the baseline in its own order is
 * what keeps two restored neighbours in their original sequence, since the
 * earlier one is on screen by the time the later one looks for its anchor.
 */
export const orderWithRestored = (
  requested: readonly string[],
  baseline: readonly string[],
  restored: ReadonlySet<string>,
): string[] => {
  const order = requested.filter((id) => !restored.has(id));
  const before = new Map(baseline.map((id, index) => [id, baseline.slice(0, index)]));
  for (const id of baseline) {
    if (!restored.has(id)) continue;
    const anchor = [...(before.get(id) ?? [])].reverse().find((previous) => order.includes(previous));
    order.splice(anchor === undefined ? 0 : order.indexOf(anchor) + 1, 0, id);
  }
  return order;
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
