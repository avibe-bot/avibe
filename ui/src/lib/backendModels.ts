import { routeableCatalogModelIds } from '../components/settings/models/backendCatalog';
import type { AgentSupply } from '../components/settings/models/types';
import { ApiError, type ApiContextType } from '../context/ApiContext';

export interface BackendModels {
  /** Selectable model identifiers for the backend. */
  models: string[];
  /** Optional display labels keyed by model identifier; values remain raw ids. */
  modelLabels?: Record<string, string>;
  /** Per-model reasoning-effort option sets; undefined when a backend has none. */
  reasoningOptions?: Record<string, { value: string; label: string }[]>;
  /** True while a background remote-catalog refresh may produce a newer snapshot. */
  catalogRefreshPending?: boolean;
}

const CATALOG_REFRESH_RETRY_DELAY_MS = 3_500;
const CATALOG_REFRESH_MAX_RETRIES = 2;

export function modelOptionLabel(model: string, labels?: Record<string, string>): string {
  return labels && Object.prototype.hasOwnProperty.call(labels, model) && labels[model]
    ? labels[model]
    : model;
}

// In Hub mode the persisted catalog is authoritative. A missing catalog uses
// the native fallback; an explicitly empty catalog remains an empty menu.
type PickerAgentCatalog = Pick<AgentSupply, 'backend' | 'mode' | 'catalog_models'>;

const hubCatalogModels = (agent: PickerAgentCatalog | null, backend: string): BackendModels | null => {
  if (!agent || agent.backend !== backend || agent.mode !== 'hub') return null;
  const catalog = agent.catalog_models ?? null;
  if (!catalog) return null;
  // A backend-owned selector such as Claude Code's Default is not routeable.
  const models = routeableCatalogModelIds(catalog);
  const rows = new Map(catalog.map((model) => [model.id, model]));
  // Model ids are user-controlled map keys. A null prototype keeps ids such as
  // "constructor" and "__proto__" from colliding with Object properties.
  const modelLabels = Object.create(null) as Record<string, string>;
  const reasoningOptions = Object.create(null) as Record<string, { value: string; label: string }[]>;
  for (const id of models) {
    const row = rows.get(id);
    if (!row) continue;
    if (row.display_name) modelLabels[id] = row.display_name;
    // Every routeable model gets a key, including an empty one: the catalog is
    // authoritative here, so "this model does not reason" must be sayable and
    // must not read as "nobody answered" and fall back to the generic ladder.
    reasoningOptions[id] = row.supports_reasoning === false
      ? []
      : row.reasoning_efforts.map((effort) => ({ value: effort, label: effort }));
  }
  return { models, modelLabels, reasoningOptions };
};

// Single source of truth for "list the selectable models for a backend",
// shared by ChatPage, the Agents detail panel, and the New Agent dialog so a
// new backend (or a fix like OpenCode's provider-prefixing) lands in one place.
//
// Ask Hub first because reading OpenCode's live catalog may start OpenCode.
//
// claude / codex expose flat model arrays. OpenCode's Direct options catalog is
// per-provider and returns RAW model ids (never provider-prefixed), so we
// flatten it into ``providerId/modelId`` keys. A projected Hub entry carries
// ``vibe_remote.model_hub_projected`` and keeps its bare canonical id instead;
// the controller derives transport addressing from that row's native protocol.
// Callers keep ``allowCustomValue`` so a model the catalog doesn't know yet can
// still be typed.
export async function fetchBackendModels(
  api: ApiContextType,
  backend: string,
): Promise<BackendModels> {
  const hub = hubCatalogModels(await api.readModelHubAgentCatalogForModelPicker(backend), backend);
  if (hub) return hub;
  if (backend === 'claude') {
    const res = await api.claudeModels();
    return {
      models: res.ok && res.models ? res.models : [],
      modelLabels: res.model_labels,
      reasoningOptions: res.reasoning_options,
      catalogRefreshPending: res.catalog_refresh_pending,
    };
  }
  if (backend === 'codex') {
    const res = await api.codexModels();
    return {
      models: res.ok && res.models ? res.models : [],
      modelLabels: res.model_labels,
      reasoningOptions: res.reasoning_options,
      catalogRefreshPending: res.catalog_refresh_pending,
    };
  }
  if (backend === 'opencode') {
    // Best-effort: this live catalog remains Owner-only because reading it may
    // start OpenCode. Lower ranks keep the existing typed-value fallback.
    const res = await api.readOpencodeOptionsForModelPicker().catch((err) => {
      if (err instanceof ApiError && err.code === 'instance_access_forbidden') return null;
      throw err;
    });
    if (!res) return { models: [] };
    const providers: unknown[] = res.ok && Array.isArray(res.data?.models?.providers)
      ? res.data.models.providers
      : [];
    const models = providers.flatMap((provider: unknown) => {
      if (!provider || typeof provider !== 'object') return [];
      const providerRecord = provider as Record<string, unknown>;
      const providerId = typeof providerRecord.id === 'string' ? providerRecord.id : '';
      if (!providerId) return [];
      const rawModels = providerRecord.models;
      const modelEntries: [string, unknown][] = Array.isArray(rawModels)
        ? rawModels.flatMap((model: unknown): [string, unknown][] => {
          if (typeof model === 'string') return [[model, model]];
          if (!model || typeof model !== 'object') return [];
          const modelId = (model as Record<string, unknown>).id;
          return typeof modelId === 'string' ? [[modelId, model]] : [];
        })
        : rawModels && typeof rawModels === 'object'
          ? Object.entries(rawModels)
          : [];
      return modelEntries
        .filter(([modelId]) => Boolean(modelId))
        .map(([modelId, modelInfo]) => {
          if (modelInfo && typeof modelInfo === 'object') {
            const metadata = (modelInfo as Record<string, unknown>).vibe_remote;
            if (
              metadata
              && typeof metadata === 'object'
              && (metadata as Record<string, unknown>).model_hub_projected === true
            ) {
              return modelId;
            }
          }
          return `${providerId}/${modelId}`;
        });
    });
    return {
      models,
      reasoningOptions: res.data?.reasoning_options,
    };
  }
  return { models: [] };
}

export function loadBackendModelsWithRefresh(
  api: ApiContextType,
  backend: string,
  onResult: (result: BackendModels) => void,
  onInitialError?: () => void,
): () => void {
  let cancelled = false;
  let delivered = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const load = async (retriesRemaining: number) => {
    try {
      const result = await fetchBackendModels(api, backend);
      if (cancelled) return;
      delivered = true;
      onResult(result);
      if (result.catalogRefreshPending && retriesRemaining > 0) {
        timer = setTimeout(() => void load(retriesRemaining - 1), CATALOG_REFRESH_RETRY_DELAY_MS);
      }
    } catch {
      if (!cancelled && !delivered) onInitialError?.();
    }
  };

  void load(CATALOG_REFRESH_MAX_RETRIES);
  return () => {
    cancelled = true;
    if (timer !== undefined) clearTimeout(timer);
  };
}
