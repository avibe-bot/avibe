// Single source of truth for reasoning-effort options, shared by ChatPage, the
// Agents detail panel, and the New Agent dialog. Mirrors the backend lists in
// modules/agents/opencode/utils.py: Codex falls back to minimal..xhigh, Claude is
// low/medium/high (+ xhigh/max on models that support it), OpenCode uses the
// broad superset. Codex/Claude model catalogs override these fallbacks.

/**
 * The unified reasoning-effort vocabulary, weakest to strongest.
 *
 * ONE ordered list for the whole UI, per the tier-provenance spec's "unified
 * vocabulary" section. Two tables used to answer overlapping questions with
 * different words — which efforts an agent BACKEND offers (below) and which
 * tiers the Model Hub editor SUGGESTS (`settings/models/tierSuggestions.ts`) —
 * and disagreed about `minimal` and `max`. Both now draw their members from
 * here, so a value can only be offered on one surface if it is sayable on the
 * other.
 *
 * It is a vocabulary, not a filter. A `reasoning_efforts` list that arrives
 * from a source is an arbitrary-string capability declaration forwarded to the
 * upstream verbatim, so a relay may legitimately declare a tier no protocol
 * ever named; those render and route unchanged. What this list bounds is
 * ordering, display, and what THIS UI proposes on its own initiative.
 *
 * `ultra` is in the ordered superset because catalog rows for gpt-5.6-sol/terra
 * declare it. Protocol-family defaults and backend fallbacks still omit it: an
 * unknown relay model must not be over-claimed.
 */
export const REASONING_EFFORTS = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'] as const;

export type ReasoningEffort = (typeof REASONING_EFFORTS)[number];

/**
 * Explicit "no reasoning" for an Agent, distinct from an unset effort.
 *
 * Unset means "let the backend choose"; on Claude that is thinking on. An Agent
 * that must not think therefore needs a value the backend can tell apart from
 * unset — `none` — which the Claude session maps to
 * `thinking: {"type": "disabled"}`. It is a sentinel, not a tier: it is absent
 * from `REASONING_EFFORTS` so it can never be suggested as a model capability,
 * and only backends that translate it appear in the list below.
 */
export const NO_REASONING_EFFORT = 'none';

// Typed against the vocabulary rather than `string[]`: a backend list that
// drifts away from it then fails to compile, which is the check the spec asks
// for stated where it cannot be forgotten.
export const EFFORT_BY_BACKEND: Record<string, ReasoningEffort[]> = {
  claude: ['low', 'medium', 'high'],
  codex: ['minimal', 'low', 'medium', 'high', 'xhigh'],
  opencode: ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
};

const DEFAULT_EFFORTS: ReasoningEffort[] = ['low', 'medium', 'high'];

export const effortOptionsFor = (backend: string): string[] => EFFORT_BY_BACKEND[backend] ?? DEFAULT_EFFORTS;

/** Rank in the unified vocabulary; unknown tokens sort after every named rung.
 *  "No reasoning" is weaker than every tier, so it ranks before them. */
const effortRank = (effort: string): number => {
  if (effort === NO_REASONING_EFFORT) return -1;
  const index = (REASONING_EFFORTS as readonly string[]).indexOf(effort);
  // A finite sentinel — not Infinity — so two unknowns subtract to 0 instead of NaN.
  return index < 0 ? REASONING_EFFORTS.length : index;
};

/** Order a selected-effort list weakest → strongest, then alphabetically for unknowns. */
export const sortEffortsByVocabulary = (efforts: readonly string[]): string[] =>
  [...efforts].sort((left, right) => {
    const delta = effortRank(left) - effortRank(right);
    return delta !== 0 ? delta : left.localeCompare(right);
  });

/** Backends whose catalog carries a "" entry: the set an inherited or custom
 *  model inherits when the catalog does not name it. */
const SHARED_DEFAULT_EFFORT_BACKENDS = new Set(['claude', 'codex']);

/** `__default__` is the IM cards' "let the backend choose" sentinel, not an
 *  effort value, so it never becomes a selectable option here. */
const selectableEfforts = (entries: { value: string; label: string }[]): string[] =>
  entries.filter((option) => option.value !== '__default__').map((option) => option.value);

/** Backends that honour an explicit "no reasoning" choice. Kept beside the
 *  resolver rather than folded into `EFFORT_BY_BACKEND` because `none` is a
 *  sentinel, not a tier a model can be declared to support — a model's own
 *  catalog answer must not be read as "this model does not reason". */
const NO_REASONING_BACKENDS = new Set(['claude']);

/** Add the Off choice ahead of the tiers, leaving an empty answer empty: a model
 *  the catalog says cannot reason has nothing to turn off. */
const withNoReasoningOption = (backend: string, efforts: string[]): string[] =>
  NO_REASONING_BACKENDS.has(backend) && efforts.length > 0 && !efforts.includes(NO_REASONING_EFFORT)
    ? [NO_REASONING_EFFORT, ...efforts]
    : efforts;

// Resolve the selectable effort values for a backend + model.
//
// A model's own entry is the answer, whichever backend it came from: the Hub
// catalog states one per model for all three, so gating the lookup by backend
// would silently discard OpenCode's answers. We do not union across models
// because that would offer unsupported pairs.
//
// A missing key and an empty entry are different answers. Missing means nobody
// has said, so Claude/Codex fall back to their catalog's "" set and every
// backend then to its static superset — that is also what `{}` means before the
// catalog loads. An empty entry under this model's own key is a statement —
// "this model does not reason" — and resolves to no efforts, not to the
// generic ladder.
//
// On a backend that can switch reasoning off, a non-empty answer also gains the
// Off sentinel ahead of its tiers (see `withNoReasoningOption`).
export function resolveEffortOptions(
  backend: string,
  model: string | null | undefined,
  reasoningOptions: Record<string, { value: string; label: string }[]> | undefined,
): string[] {
  const modelKey = model ?? '';
  if (reasoningOptions && Object.prototype.hasOwnProperty.call(reasoningOptions, modelKey)) {
    return withNoReasoningOption(backend, selectableEfforts(reasoningOptions[modelKey]));
  }
  const shared = SHARED_DEFAULT_EFFORT_BACKENDS.has(backend)
    && reasoningOptions
    && Object.prototype.hasOwnProperty.call(reasoningOptions, '')
    ? reasoningOptions['']
    : undefined;
  const values = shared ? selectableEfforts(shared) : [];
  return withNoReasoningOption(backend, values.length ? values : effortOptionsFor(backend));
}

export function isEffortSupported(
  backend: string,
  model: string | null | undefined,
  effort: string | null | undefined,
  reasoningOptions: Record<string, { value: string; label: string }[]> | undefined,
): boolean {
  return !effort || resolveEffortOptions(backend, model, reasoningOptions).includes(effort);
}
