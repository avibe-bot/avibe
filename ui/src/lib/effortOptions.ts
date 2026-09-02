// Single source of truth for reasoning-effort options, shared by ChatPage, the
// Agents detail panel, and the New Agent dialog. Mirrors the backend lists in
// modules/agents/opencode/utils.py: Codex falls back to minimal..xhigh, Claude is
// low/medium/high (+ xhigh/max on models that support it), OpenCode uses the
// broad superset. Codex/Claude model catalogs override these fallbacks.
export const EFFORT_BY_BACKEND: Record<string, string[]> = {
  claude: ['low', 'medium', 'high'],
  codex: ['minimal', 'low', 'medium', 'high', 'xhigh'],
  opencode: ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
};

const DEFAULT_EFFORTS = ['low', 'medium', 'high'];

export const effortOptionsFor = (backend: string): string[] => EFFORT_BY_BACKEND[backend] ?? DEFAULT_EFFORTS;

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
