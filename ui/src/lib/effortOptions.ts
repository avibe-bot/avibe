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

const PER_MODEL_EFFORT_BACKENDS = new Set(['claude', 'codex']);

// Resolve the selectable effort values for a backend + model. Claude and Codex
// consult their catalog maps; other backends always use their static superset so
// stale state cannot leak across backend switches. A known model uses its own
// set, while an inherited or custom model uses the catalog's "" default set.
// We do not union across models because that would offer unsupported pairs.
// ``reasoningOptions`` may be {} before the catalog loads, which yields the
// backend fallback until the immediate snapshot arrives.
export function resolveEffortOptions(
  backend: string,
  model: string | null | undefined,
  reasoningOptions: Record<string, { value: string; label: string }[]> | undefined,
): string[] {
  if (PER_MODEL_EFFORT_BACKENDS.has(backend) && reasoningOptions) {
    const perModel = reasoningOptions[model ?? ''] ?? reasoningOptions[''];
    const values = perModel?.filter((o) => o.value !== '__default__').map((o) => o.value);
    if (values && values.length) return values;
  }
  return effortOptionsFor(backend);
}

export function isEffortSupported(
  backend: string,
  model: string | null | undefined,
  effort: string | null | undefined,
  reasoningOptions: Record<string, { value: string; label: string }[]> | undefined,
): boolean {
  return !effort || resolveEffortOptions(backend, model, reasoningOptions).includes(effort);
}
