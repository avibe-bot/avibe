import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { routeableCatalogModelIds } from '../components/settings/models/backendCatalog';
import { modelHubCatalogPath } from '../components/settings/models/modelHubRoutes';
import type { EffortMenu, PickerAgentCatalog } from '../components/settings/models/types';
import { ApiError, useApi, type ApiContextType } from '../context/ApiContext';
import { useInstanceAuthorization } from '../context/InstanceAuthorizationContext';
import { useRouteSurfaceActive } from './routeSurfaceActivity';
import { settingsOverlayContinuationState } from './settingsOverlay';
import { getBackendUiMeta } from './agentBackends';

export interface BackendModels {
  /** Selectable model identifiers for the backend. */
  models: string[];
  /** Optional display labels keyed by model identifier; values remain raw ids. */
  modelLabels?: Record<string, string>;
  /** Per-model reasoning-effort option sets; undefined when a backend has none.
   *  A Hub catalog leaves out every effort routing would refuse on every hop. */
  reasoningOptions?: Record<string, { value: string; label: string }[]>;
  /** Per model, what routing would do with its efforts now (Model Hub's
   *  `effort_menu`): the efforts left out above, and by Source id the hops that
   *  refuse one the others still serve. Hub catalogs only. */
  effortMenus?: Record<string, EffortMenu>;
  /** The models a turn would run on at its vendor's fast tier (Model Hub's
   *  `fast_mode`). Hub catalogs only: a Direct backend offers none. */
  fastModels?: string[];
  /** True while a background remote-catalog refresh may produce a newer snapshot. */
  catalogRefreshPending?: boolean;
  /** Model Hub gave no answer (the read failed, or the server predates the read),
   *  so these are the native list. A Direct-mode answer is an answer: absent. */
  hubUnread?: true;
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
const isHubAgent = (agent: PickerAgentCatalog | null, backend: string): agent is PickerAgentCatalog =>
  agent !== null && agent.backend === backend && agent.mode === 'hub';

const hubCatalogModels = (agent: PickerAgentCatalog | null, backend: string): BackendModels | null => {
  if (!isHubAgent(agent, backend)) return null;
  const catalog = agent.catalog_models ?? null;
  if (!catalog) return null;
  // A backend-owned selector such as Claude Code's Default is not routeable.
  const models = routeableCatalogModelIds(catalog);
  const rows = new Map(catalog.map((model) => [model.id, model]));
  // Model ids are user-controlled map keys. A null prototype keeps ids such as
  // "constructor" and "__proto__" from colliding with Object properties.
  const modelLabels = Object.create(null) as Record<string, string>;
  const reasoningOptions = Object.create(null) as Record<string, { value: string; label: string }[]>;
  const effortMenus = Object.create(null) as Record<string, EffortMenu>;
  const fastModels: string[] = [];
  for (const id of models) {
    const row = rows.get(id);
    if (!row) continue;
    if (row.display_name) modelLabels[id] = row.display_name;
    // Model Hub states what routing would do with each effort now (`effort_menu`):
    // one every hop refuses would only fail, so it is not offered.
    const omitted = new Set(row.effort_menu?.omitted ?? []);
    // Every routeable model gets a key, including an empty one: the catalog is
    // authoritative here, so "this model does not reason" must be sayable and
    // must not read as "nobody answered" and fall back to the generic ladder.
    reasoningOptions[id] = row.supports_reasoning === false
      ? []
      : row.reasoning_efforts.filter((effort) => !omitted.has(effort)).map((effort) => ({ value: effort, label: effort }));
    if (row.effort_menu) effortMenus[id] = row.effort_menu;
    if (row.fast_mode === true) fastModels.push(id);
  }
  return { models, modelLabels, reasoningOptions, effortMenus, fastModels };
};

/**
 * Whether `result` may replace what a surface shows for a backend. Model Hub
 * answers with effort menus; a Hub read that fails comes back as the native
 * list (`hubUnread`), which says nothing about Hub routing and would offer
 * efforts Hub refuses. So once a backend has answered from Hub in a surface's
 * activation, an unread Hub does not replace it: the last answer stands, and the
 * next read asks Hub again. A confirmed Direct answer does replace it.
 */
export const supersedesHubAnswer = (result: BackendModels, knownHub: boolean): boolean =>
  !knownHub || result.effortMenus !== undefined || result.hubUnread !== true;

/**
 * Model Hub's catalog answer for a backend: null when Hub does not serve it,
 * `'unread'` when Hub gave no answer. It starts no CLI.
 *
 * A Hub effort menu follows capability evidence that any turn can change
 * (#2606). The route picker and the Agents page read it again as their menu
 * opens or the effort field is approached. The route picker holds controls
 * while that read is in flight; the Agents page holds model changes and queues
 * the latest effort choice, validating it against the answer before saving.
 * The New Agent dialog writes on Create, so Create reads this
 * answer again and creates only an effort it still offers, and nothing when Hub
 * cannot answer. A write made outside those (a model changed before any read
 * started, or after a read that came back unread) uses the last answer; a stale
 * effort then fails at the turn as `effort_unsupported`, which records the
 * evidence the next read uses.
 */
export async function readHubBackendModels(
  api: ApiContextType,
  backend: string,
): Promise<BackendModels | null | 'unread'> {
  const agent = await api.readModelHubAgentCatalogForModelPicker(backend);
  return agent === null ? 'unread' : hubCatalogModels(agent, backend);
}

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
  const agent = await api.readModelHubAgentCatalogForModelPicker(backend);
  const hub = hubCatalogModels(agent, backend);
  if (hub) return hub;
  const native = await nativeBackendModels(api, backend);
  return agent === null ? { ...native, hubUnread: true } : native;
}

/**
 * Just the display labels of a backend's models, for a closed picker that names
 * the current model. Reads only what is already on disk: Model Hub's persisted
 * catalog, or Claude/Codex's cached catalog snapshot. OpenCode's live list is
 * never read here — it may start OpenCode, and it carries no labels anyway.
 */
/**
 * The models a backend's turns would run on at their vendor's fast tier, read
 * from Model Hub's persisted catalog only. A backend Hub does not serve, or
 * whose catalog it cannot answer, offers none.
 */
export async function fetchBackendFastModels(api: ApiContextType, backend: string): Promise<string[]> {
  return hubCatalogModels(await api.readModelHubAgentCatalogForModelPicker(backend), backend)?.fastModels ?? [];
}

export async function fetchBackendModelLabels(
  api: ApiContextType,
  backend: string,
): Promise<Record<string, string>> {
  const hub = hubCatalogModels(await api.readModelHubAgentCatalogForModelPicker(backend), backend);
  if (hub) return hub.modelLabels ?? {};
  if (backend !== 'claude' && backend !== 'codex') return {};
  return (await nativeBackendModels(api, backend)).modelLabels ?? {};
}

async function nativeBackendModels(api: ApiContextType, backend: string): Promise<BackendModels> {
  if (!getBackendUiMeta(backend).capabilities.supports_cli) return { models: [] };
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

/**
 * A model picker's "Add model" exit: opens this backend's catalog in the Model
 * Hub, or is null when the picker shows none. Only a Model Hub backend has a
 * catalog to add to, and the Model Hub is an Instance Owner surface that sends
 * anyone else home.
 *
 * The one owner of that answer. It comes from a read made while the surface is
 * showing and is dropped the moment it may be stale — the surface is covered
 * (Settings may switch the backend to Direct), the backend changes, or `enabled`
 * turns off — so no exit outlives the read that justified it, and a failed read
 * shows none. `enabled` lets a picker read only while its menu is open.
 *
 * The exit is a detour to come back from, so it records where it starts, on a
 * phone too, where Settings otherwise keeps only the Workbench home mounted
 * behind it. From a picker already inside Settings it keeps the visit's origin.
 */
export function useAddModelExit(backend: string, enabled = true): (() => void) | null {
  const api = useApi();
  const navigate = useNavigate();
  const location = useLocation();
  const { capabilities } = useInstanceAuthorization();
  const surfaceActive = useRouteSurfaceActive();
  const readable = enabled && surfaceActive && Boolean(backend) && capabilities.can_manage_instance;
  const [hubBackend, setHubBackend] = useState<string | null>(null);
  useEffect(() => {
    if (!readable) return;
    let cancelled = false;
    void api.readModelHubAgentCatalogForModelPicker(backend).then((agent) => {
      if (!cancelled && isHubAgent(agent, backend)) setHubBackend(backend);
    });
    return () => {
      cancelled = true;
      setHubBackend(null);
    };
  }, [api, backend, readable]);
  if (!readable || hubBackend !== backend) return null;
  return () => navigate(modelHubCatalogPath(backend), { state: settingsOverlayContinuationState(location) });
}

/**
 * Read a backend's model list, then re-read while a remote-catalog refresh is
 * pending. A caller whose surface stays mounted under Settings treats a Settings
 * visit as invalidating what it read: it drops those rows when
 * `useRouteSurfaceActive()` turns false and reads again when it turns true, since
 * the Model Hub there edits the list and may change which catalog supplies it.
 */
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
