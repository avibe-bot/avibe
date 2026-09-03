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

/** Rank in the unified vocabulary; unknown tokens sort after every named rung. */
const effortRank = (effort: string): number => {
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
export function resolveEffortOptions(
  backend: string,
  model: string | null | undefined,
  reasoningOptions: Record<string, { value: string; label: string }[]> | undefined,
): string[] {
  const modelKey = model ?? '';
  if (reasoningOptions && Object.prototype.hasOwnProperty.call(reasoningOptions, modelKey)) {
    return selectableEfforts(reasoningOptions[modelKey]);
  }
  const shared = SHARED_DEFAULT_EFFORT_BACKENDS.has(backend)
    && reasoningOptions
    && Object.prototype.hasOwnProperty.call(reasoningOptions, '')
    ? reasoningOptions['']
    : undefined;
  const values = shared ? selectableEfforts(shared) : [];
  return values.length ? values : effortOptionsFor(backend);
}

export function isEffortSupported(
  backend: string,
  model: string | null | undefined,
  effort: string | null | undefined,
  reasoningOptions: Record<string, { value: string; label: string }[]> | undefined,
): boolean {
  return !effort || resolveEffortOptions(backend, model, reasoningOptions).includes(effort);
}
