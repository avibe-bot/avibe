// The catalog half of the registered mirror: what this client promises to send,
// and what it promises not to drop on the way in.
//
// Two anchors, on purpose. Where `docs/plans/model-hub-contracts/` states a
// shape, the schema is the authority and these tests read it — a field added
// there fails here rather than reaching a projection nobody renders. Where it
// does not, the property is the anchor instead: the real client is driven over
// a stubbed wire, and a typed fixture with no optional field left out is
// asserted to survive the round trip intact.
//
// The candidates read moved from the second anchor to the first when #1837
// merged its authority files, so `ModelCandidate` is now read out of
// `api-response.schema.json` rather than described here twice.
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { apiFailure, CANDIDATE_ORIGINS, modelsApi } from './modelsApi';
import {
  BACKEND_MODEL_INPUT_MODALITIES,
  BACKEND_MODEL_OUTPUT_MODALITIES,
  CONTRACT_VERSION,
  type BackendModel,
  type BackendModelsPut,
  type ModelCandidate,
  type ModelsDevMatch,
  type RouteHopRef,
} from './types';

const contract = (name: string): Record<string, never> =>
  JSON.parse(
    readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), '../../../../..', 'docs/plans/model-hub-contracts', name),
      'utf8',
    ),
  );

/**
 * One canned answer, and a record of the request that asked for it.
 *
 * The subject is the shipped client, not a second mapper written here: a double
 * that re-states the projection would agree with itself no matter what the real
 * one does with a field.
 */
const stubFetch = (status: number, body: unknown) => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(
    JSON.stringify(body),
    { status, headers: { 'Content-Type': 'application/json' } },
  ));
  vi.stubGlobal('document', { cookie: 'vibe_csrf_token=test-token' });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
};

const MODEL: BackendModel = {
  id: 'anthropic/claude-opus-4-6',
  display_name: 'Claude Opus 4.6',
  origin: 'models_dev',
  models_dev_id: 'anthropic/claude-opus-4-6',
  context_window: 200_000,
  max_output_tokens: 64_000,
  input_modalities: [...BACKEND_MODEL_INPUT_MODALITIES],
  output_modalities: [...BACKEND_MODEL_OUTPUT_MODALITIES],
  supports_tools: true,
  supports_reasoning: null,
  reasoning_efforts: ['low', 'high'],
  locked: false,
  routeable: true,
};

const HOP_REF: RouteHopRef = {
  backend: 'opencode',
  menu_model: 'glm-5.2',
  source_id: 'src_relay0001',
  model_id: 'glm-5.2-air',
  position: 2,
};

/** Every field the projection states, optional one included — so the schema's
 *  closed-object set equality below is a relation in both directions. */
const CANDIDATE: Required<ModelCandidate> = {
  id: 'glm-5.2',
  display_name: 'GLM 5.2',
  reasoning_efforts: ['low', 'high'],
  suppliers: [{ source_id: 'src_relay0001', source_name: 'relay.example', model_id: 'glm-5.2-air' }],
  origin: 'provider',
  group_if_removed: 'providers',
  native_protocol: 'openai_responses',
};

const MATCH: ModelsDevMatch = {
  provider_id: 'zhipuai',
  provider_name: 'Zhipu AI',
  model_id: 'glm-5.2',
  models_dev_id: 'zhipuai/glm-5.2',
  display_name: 'GLM 5.2',
  context_window: 128_000,
  max_output_tokens: 8_192,
  input_modalities: ['text', 'image'],
  output_modalities: ['text'],
  supports_tools: true,
  supports_reasoning: null,
  reasoning_efforts: ['low'],
  native_protocol: 'openai_responses',
  first_party: true,
};

/** A save that removes a routed row AND adds a candidate: every optional field
 *  of the write is present at once, so nothing can pass by being absent. */
const PUT: BackendModelsPut = {
  baseline: [MODEL],
  models: [MODEL, { ...MODEL, id: 'glm-5.2', models_dev_id: null, origin: 'manual' }],
  force: true,
  would_remove_hops: [HOP_REF],
  would_interrupt: [{ backend: 'opencode', model_id: 'glm-5.2', agents: ['pm'] }],
  expected_suppliers: { 'glm-5.2': [{ source_id: 'src_relay0001', model_id: 'glm-5.2-air' }] },
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('backend model catalog contract', () => {
  it('mirrors every field and vocabulary backend-model.schema.json states', () => {
    const schema = contract('backend-model.schema.json') as unknown as {
      required: string[];
      additionalProperties: boolean;
      properties: Record<string, { enum?: string[]; items?: { enum: string[] } }>;
    };

    // The schema closes its object and requires every property, so set equality
    // is the whole relation in both directions: a field it gains is missing
    // here, and one this mirror invents is not in it.
    expect(schema.additionalProperties).toBe(false);
    expect(new Set(Object.keys(MODEL))).toEqual(new Set(schema.required));

    // One row carrying every stated modality, so a member added to either
    // vocabulary fails to typecheck against the mirror's own constant.
    expect(new Set(MODEL.input_modalities)).toEqual(new Set(schema.properties.input_modalities.items?.enum));
    expect(new Set(MODEL.output_modalities)).toEqual(new Set(schema.properties.output_modalities.items?.enum));
    // The origin vocabulary, both directions now that `provider` has landed in
    // the schema: the runtime set the candidates read validates against is the
    // one the contract states, so a creation path either side gains fails here
    // instead of being read as the group's fallback.
    expect(new Set(CANDIDATE_ORIGINS)).toEqual(new Set(schema.properties.origin.enum));
    expect(new Set(CANDIDATE_ORIGINS)).toContain(MODEL.origin);
  });

  it('mirrors the candidate projection api-response.schema.json now states', () => {
    const schema = contract('api-response.schema.json') as unknown as {
      definitions: {
        ModelCandidate: {
          required: string[];
          additionalProperties: boolean;
          properties: Record<string, { anyOf?: { enum?: string[] }[] }>;
        };
        AddableModelCandidate: { allOf: [unknown, { not: { required: string[] } }] };
        AgentModelCandidatesResponse: { properties: { contract_version: { const: number } } };
      };
    };
    const candidate = schema.definitions.ModelCandidate;

    // The object is closed, so set equality over the fixture's keys is the
    // whole relation: a property the schema gains is missing from the mirror,
    // and one the mirror invents is not in the schema. `Required<…>` is what
    // makes the optional field participate.
    expect(candidate.additionalProperties).toBe(false);
    expect(new Set(Object.keys(CANDIDATE)))
      .toEqual(new Set([...candidate.required, ...Object.keys(candidate.properties)]));
    // And what the mirror calls optional is what the schema leaves out of
    // `required`, rather than a second opinion about the same field.
    expect(new Set(candidate.required))
      .toEqual(new Set(Object.keys(CANDIDATE).filter(
        (field) => !['group_if_removed', 'native_protocol'].includes(field),
      )));

    // The re-entry vocabulary, and the reason it is a separate field: the schema
    // forbids it on the two addable groups, because a candidate not in the list
    // has no way back to state.
    expect(new Set(candidate.properties.group_if_removed?.anyOf?.[0]?.enum))
      .toEqual(new Set(['builtin', 'providers']));
    expect(schema.definitions.AddableModelCandidate.allOf[1].not.required).toEqual(['group_if_removed']);
    expect(CONTRACT_VERSION).toBe(schema.definitions.AgentModelCandidatesResponse.properties.contract_version.const);
  });

  it('mirrors the plan hop reference and the version the refusal schema registers', () => {
    const schema = contract('guard-refusal.schema.json') as unknown as {
      properties: { contract_version: { const: number } };
      definitions: { RouteHopRef: { required: string[]; additionalProperties: boolean } };
    };
    const hopRef = schema.definitions.RouteHopRef;

    expect(hopRef.additionalProperties).toBe(false);
    expect(new Set(Object.keys(HOP_REF))).toEqual(new Set(hopRef.required));
    expect(CONTRACT_VERSION).toBe(schema.properties.contract_version.const);
  });

  it('hands the picker each candidate group whole, from the registered path', async () => {
    const groups = {
      builtin: [{ ...CANDIDATE, id: 'gpt-6-codex', origin: 'builtin' as const, suppliers: [] }],
      providers: [CANDIDATE],
      in_list: [{ ...CANDIDATE, id: 'kimi-k3', origin: 'manual' as const }],
    };
    const fetchMock = stubFetch(200, { ok: true, contract_version: CONTRACT_VERSION, candidates: groups });

    const read = await modelsApi.getAgentModelCandidates('opencode');

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/models/agents/opencode/models/candidates');
    // Deep equality against typed fixtures is the property, and the schema case
    // above is what keeps the fixtures honest: every field the contract states
    // is on `CANDIDATE`, so a field the projection gains is asserted to survive
    // the read rather than being dropped on the way in.
    expect(read).toEqual(groups);
  });

  it('keeps every models.dev field the ranking is stated in', async () => {
    stubFetch(200, { ok: true, contract_version: CONTRACT_VERSION, matches: [MATCH] });

    const [read] = await modelsApi.searchModelsDev('glm');

    expect(read).toEqual(MATCH);
    // `first_party` is optional in the mirror — a server that predates it leaves
    // the ranking to the order it served — so its survival is asserted rather
    // than inferred from a fixture that could have omitted it.
    expect(read?.first_party).toBe(true);
  });

  it('sends the guarded tail and the displayed suppliers verbatim', async () => {
    const fetchMock = stubFetch(200, { ok: true, contract_version: CONTRACT_VERSION, agent: { backend: 'opencode' } });

    await modelsApi.putAgentModels('opencode', PUT);

    const [path, init] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe('/api/models/agents/opencode/models');
    expect(init?.method).toBe('PUT');
    // The server stores this request literally, so the client's only correct
    // transformation of the body is none.
    expect(JSON.parse(String(init?.body))).toEqual(PUT);
  });

  it('projects a stale-candidate refusal and a guarded plan as different answers', async () => {
    const refusal = async (body: unknown) => {
      stubFetch(409, body);
      const failure = await modelsApi.putAgentModels('opencode', PUT).then(
        () => null,
        (error: unknown) => apiFailure(error),
      );
      expect(failure, 'a 409 must reject as one of ours').not.toBeNull();
      return failure as NonNullable<typeof failure>;
    };

    const changed = { 'glm-5.2': [{ source_id: 'src_relay0002', model_id: 'glm-5.2' }] };
    const stale = await refusal({
      ok: false,
      contract_version: CONTRACT_VERSION,
      error: 'candidate_suppliers_changed',
      // Registered on this shape too, because it is a `ModelHubError` like any
      // other. The copy it keys is the backend lane's, but the projection that
      // has to carry it here is this client's.
      detail: 'candidate_suppliers_changed.detail',
      changed,
    });

    // Each refusal is answerable from its own field alone. That is what lets the
    // picker refresh its chips and re-ask on one and echo a plan back on the
    // other, without reading the code to decide which arrays to trust.
    expect(stale.code).toBe('candidate_suppliers_changed');
    expect(stale.detail).toBe('candidate_suppliers_changed.detail');
    expect(stale.changedSuppliers).toEqual(changed);
    expect(stale.wouldRemoveHops).toEqual([]);
    expect(stale.serverNamed).toBe(true);

    const guarded = await refusal({
      ok: false,
      contract_version: CONTRACT_VERSION,
      error: 'backend_model_in_route',
      would_remove_hops: [HOP_REF],
      would_interrupt: [{ backend: 'opencode', model_id: 'glm-5.2', agents: ['pm'] }],
    });

    expect(guarded.code).toBe('backend_model_in_route');
    expect(guarded.wouldRemoveHops).toEqual([HOP_REF]);
    expect(guarded.wouldInterrupt).toHaveLength(1);
    expect(guarded.changedSuppliers).toEqual({});
  });
});
