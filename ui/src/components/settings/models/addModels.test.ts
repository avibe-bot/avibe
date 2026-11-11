import { describe, expect, it, vi } from 'vitest';

import { addModelsToBackend } from './addModels';
import { chosenCandidate, typedPick } from './backendCatalog';
import { ApiCallError } from './modelsApi';
import type { AgentSupply, BackendModel, ModelCandidate } from './types';

const row = (id: string): BackendModel => ({
  id, display_name: null, origin: 'provider', models_dev_id: null, context_window: null, max_output_tokens: null,
  input_modalities: ['text'], output_modalities: ['text'], supports_tools: true, supports_reasoning: false,
  reasoning_efforts: [], locked: false, routeable: true,
});
const supply = (ids: string[]): AgentSupply => ({
  backend: 'claude', cli_present: true, mode: 'hub', menu_kind: 'fixed', catalog_models: ids.map(row),
} as AgentSupply);
const offered = (id: string): ModelCandidate => ({
  id, display_name: null, reasoning_efforts: [], origin: 'provider',
  suppliers: [{ source_id: 'src_a', source_name: 'Relay', model_id: id }],
});
const refused = (detail: string) => new ApiCallError('invalid_request', detail, true);

describe('addModelsToBackend', () => {
  it('appends what the list lacks, in pick order, promising the suppliers each pick showed', async () => {
    const api = {
      getAgentSources: vi.fn().mockResolvedValue(supply(['held'])),
      putAgentModels: vi.fn().mockResolvedValue(supply(['held', 'new-a', 'new-b'])),
    };
    const result = await addModelsToBackend(api, 'claude', [
      chosenCandidate(offered('new-a')), chosenCandidate(offered('held')), typedPick('new-b', 'claude'),
    ]);

    expect(result).toMatchObject({ ok: true, ids: ['new-a', 'held', 'new-b'] });
    const [, body] = api.putAgentModels.mock.calls[0];
    expect(body.baseline.map((model: BackendModel) => model.id)).toEqual(['held']);
    expect(body.models.map((model: BackendModel) => model.id)).toEqual(['held', 'new-a', 'new-b']);
    expect(body.expected_suppliers).toEqual({ 'new-a': [{ source_id: 'src_a', model_id: 'new-a' }], 'new-b': [] });
  });

  it('writes nothing when every pick is already in the list', async () => {
    const api = { getAgentSources: vi.fn().mockResolvedValue(supply(['held'])), putAgentModels: vi.fn() };
    expect(await addModelsToBackend(api, 'claude', [chosenCandidate(offered('held'))])).toMatchObject({ ok: true, ids: ['held'] });
    expect(api.putAgentModels).not.toHaveBeenCalled();
  });

  it('replays an add once onto a list that changed under it', async () => {
    const api = {
      getAgentSources: vi.fn().mockResolvedValueOnce(supply([])).mockResolvedValueOnce(supply(['theirs'])),
      putAgentModels: vi.fn().mockRejectedValueOnce(refused('modelHub.errors.backend_model_conflict'))
        .mockResolvedValueOnce(supply(['theirs', 'mine'])),
    };
    expect(await addModelsToBackend(api, 'claude', [typedPick('mine', 'claude')])).toMatchObject({ ok: true });
    expect(api.putAgentModels.mock.calls[1][1].models.map((model: BackendModel) => model.id)).toEqual(['theirs', 'mine']);
  });

  it('names a refusal, and settles an unanswered write by reading the list back', async () => {
    const named = {
      getAgentSources: vi.fn().mockResolvedValue(supply([])),
      putAgentModels: vi.fn().mockRejectedValue(refused('modelHub.errors.candidate_suppliers_changed')),
    };
    expect(await addModelsToBackend(named, 'claude', [chosenCandidate(offered('x'))]))
      .toEqual({ ok: false, reason: 'settings.models.gateway.catalog.saveSuppliersChanged' });

    const lost = {
      getAgentSources: vi.fn().mockResolvedValueOnce(supply([])).mockResolvedValueOnce(supply(['x'])),
      putAgentModels: vi.fn().mockRejectedValue(new TypeError('Failed to fetch')),
    };
    expect(await addModelsToBackend(lost, 'claude', [chosenCandidate(offered('x'))])).toMatchObject({ ok: true, ids: ['x'] });
  });
});
