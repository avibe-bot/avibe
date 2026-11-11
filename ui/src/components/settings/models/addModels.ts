// Adding picked models to a backend's list in one write, for the entries that
// have no list of their own on screen to save later: the routes page and the
// model pickers in chat, the Agents page and project and channel routing.
//
// The list is read for its baseline, the picks the list lacks are appended in
// pick order, and the PUT carries the suppliers each pick displayed. Nothing is
// removed or reordered, so the route guard never has anything to ask.
import type { TranslationKey } from '@/i18n/types';

import { draftRowFor, readBackendCatalogBaseline, type ChosenCandidate } from './backendCatalog';
import { apiFailure, type ModelsApi } from './modelsApi';
import { catalogSaveFailureKey } from './serverCopy';
import type { AgentBackend, AgentSupply } from './types';

export type AddModelsResult =
  | { ok: true; agent: AgentSupply; ids: string[] }
  | { ok: false; reason: TranslationKey };

const CONFLICT = 'modelHub.errors.backend_model_conflict';
const SAVE_FAILED: TranslationKey = 'settings.models.gateway.catalog.saveFailed';

export async function addModelsToBackend(
  api: Pick<ModelsApi, 'getAgentSources' | 'putAgentModels'>,
  backend: AgentBackend,
  picks: readonly ChosenCandidate[],
): Promise<AddModelsResult> {
  const ids = [...new Set(picks.map((pick) => pick.candidate.id))];
  // One more attempt for a list that changed between the read and the write:
  // an add carries no decision about the rows someone else just wrote.
  for (let attempt = 0; attempt < 2; attempt += 1) {
    let baseline;
    try {
      baseline = await readBackendCatalogBaseline(api, backend);
    } catch {
      return { ok: false, reason: SAVE_FAILED };
    }
    if (baseline.models === null) return { ok: false, reason: SAVE_FAILED };
    const held = baseline.models;
    const fresh = picks.filter((pick, index) => !held.some((model) => model.id === pick.candidate.id)
      && picks.findIndex((other) => other.candidate.id === pick.candidate.id) === index);
    if (fresh.length === 0) return { ok: true, agent: baseline.agent, ids };
    try {
      const agent = await api.putAgentModels(backend, {
        baseline: held,
        models: [...held, ...fresh.map((pick) => draftRowFor(pick, [], held))],
        // Only what a row showed is promised; a catalog or typed pick showed nothing.
        expected_suppliers: Object.fromEntries(fresh.flatMap((pick) => (
          pick.expected_suppliers ? [[pick.candidate.id, pick.expected_suppliers] as const] : []))),
      });
      return { ok: true, agent, ids };
    } catch (error) {
      const failure = apiFailure(error);
      if (failure?.serverNamed) {
        if (failure.detail === CONFLICT && attempt === 0) continue;
        return { ok: false, reason: catalogSaveFailureKey(failure.detail) };
      }
      // An answer that never arrived: the list itself says whether the write landed.
      try {
        const observed = await readBackendCatalogBaseline(api, backend);
        if (observed.models && ids.every((id) => observed.models!.some((model) => model.id === id))) {
          return { ok: true, agent: observed.agent, ids };
        }
      } catch {
        // Unknown either way; the failure below is what the user can act on.
      }
      return { ok: false, reason: SAVE_FAILED };
    }
  }
  return { ok: false, reason: catalogSaveFailureKey(CONFLICT) };
}
