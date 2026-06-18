import type { ApiContextType } from '../context/ApiContext';

export interface BackendModels {
  /** Selectable model identifiers for the backend. */
  models: string[];
  /** Optional display labels keyed by model identifier; values remain raw ids. */
  modelLabels?: Record<string, string>;
  /** Per-model reasoning-effort option sets (Claude only); undefined elsewhere. */
  reasoningOptions?: Record<string, { value: string; label: string }[]>;
}

export function modelOptionLabel(model: string, labels?: Record<string, string>): string {
  return labels?.[model] || model;
}

// Single source of truth for "list the selectable models for a backend",
// shared by ChatPage, the Agents detail panel, and the New Agent dialog so a
// new backend (or a fix like OpenCode's provider-prefixing) lands in one place.
//
// claude / codex expose flat model arrays. OpenCode's catalog is per-provider
// and the provider endpoint returns RAW model ids (never provider-prefixed), so
// we flatten it into ``providerId/modelId`` keys — ALWAYS provider-prefixed,
// even when the raw id itself contains "/" (e.g. OpenRouter's
// ``anthropic/claude-*`` must become ``openrouter/anthropic/claude-*``). The
// OpenCode adapter resolves the override by splitting on the FIRST "/" into
// {providerID, modelID}, so the prefix is required for the selection to bind to
// the right provider. Callers keep ``allowCustomValue`` so a model the catalog
// doesn't know yet can still be typed.
export async function fetchBackendModels(
  api: ApiContextType,
  backend: string,
): Promise<BackendModels> {
  if (backend === 'claude') {
    const res = await api.claudeModels();
    return {
      models: res.ok && res.models ? res.models : [],
      modelLabels: res.model_labels,
      reasoningOptions: res.reasoning_options,
    };
  }
  if (backend === 'codex') {
    const res = await api.codexModels();
    return { models: res.ok && res.models ? res.models : [] };
  }
  if (backend === 'opencode') {
    const res = await api.getOpencodeProviders();
    const models = (res.providers ?? []).filter((p) => p.configured).flatMap((p) =>
      (p.models ?? []).map((m) => `${p.id}/${m}`),
    );
    return { models };
  }
  return { models: [] };
}
