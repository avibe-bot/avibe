import { describe, expect, it } from 'vitest';

import {
  ApiCallError,
  MockStore,
  UncontractedMockTransitionError,
} from './modelsApi';
import type { RouteHopRef } from './types';

const referencesTo = (store: MockStore, sourceId: string): RouteHopRef[] =>
  store.agents.flatMap((agent) =>
    Object.entries(agent.routes ?? {}).flatMap(([menuModel, route]) =>
      route.hops.flatMap((hop, index) => hop.source_id === sourceId
        ? [{
            backend: agent.backend,
            menu_model: menuModel,
            source_id: hop.source_id,
            model_id: hop.model_id,
            position: index + 1,
          }]
        : [])));

describe('Model Hub mock deletion contract', () => {
  it('reports and removes every seeded route reference to a deleted source', async () => {
    const seeded = new MockStore();
    const routedSourceIds = new Set(
      seeded.agents.flatMap((agent) =>
        Object.values(agent.routes ?? {}).flatMap((route) =>
          route.hops.map((hop) => hop.source_id))),
    );
    expect(routedSourceIds.size).toBeGreaterThan(0);

    for (const sourceId of routedSourceIds) {
      const store = new MockStore();
      const expected = referencesTo(store, sourceId);
      let refusal: ApiCallError | null = null;
      try {
        store.deleteSource(sourceId);
      } catch (error) {
        if (error instanceof ApiCallError) refusal = error;
        else throw error;
      }

      expect(refusal, sourceId).not.toBeNull();
      expect(refusal?.code, sourceId).toBe('source_in_route_chain');
      expect(refusal?.wouldRemoveHops, sourceId).toEqual(expected);
      const answer = await store.deleteSource(sourceId, {
        force: true,
        would_remove_hops: refusal?.wouldRemoveHops ?? [],
        would_interrupt: refusal?.wouldInterrupt ?? [],
      });

      expect(answer.removed_hops, sourceId).toEqual(expected);
      expect(referencesTo(store, sourceId), sourceId).toEqual([]);
      for (const agent of store.agents) {
        expect(agent.sources?.order ?? [], `${sourceId}:${agent.backend}`).not.toContain(sourceId);
      }
    }
  });
});

describe('Model Hub mock endpoint-edit contract', () => {
  it('reports and removes every invalidated route reference for every seeded routed Hub key', async () => {
    const seeded = new MockStore();
    const routedSourceIds = new Set(
      seeded.agents.flatMap((agent) =>
        Object.values(agent.routes ?? {}).flatMap((route) =>
          route.hops.map((hop) => hop.source_id))),
    );
    const editableRoutedSourceIds = seeded.sources
      .filter((source) => routedSourceIds.has(source.id)
        && source.kind === 'api_key'
        && source.supply_channel === 'hub'
        && source.credential_ref)
      .map((source) => source.id);
    expect(editableRoutedSourceIds.length).toBeGreaterThan(0);

    for (const sourceId of editableRoutedSourceIds) {
      const store = new MockStore();
      const beforeReferences = referencesTo(store, sourceId);
      const beforeRoutes = new Map(store.agents.flatMap((agent) =>
        Object.entries(agent.routes ?? {}).map(([menuModel, route]) => [
          `${agent.backend}\u0000${menuModel}`,
          route.hops.map((hop) => ({ ...hop })),
        ])));
      const beforeOrders = new Map(store.agents.map((agent) => [
        agent.backend,
        [...(agent.sources?.order ?? [])],
      ]));
      const base_url = `https://retarget-${sourceId}.example/v1`;
      let refusal: ApiCallError | null = null;
      let answer;
      try {
        answer = await store.patchSource(sourceId, { base_url });
      } catch (error) {
        if (error instanceof ApiCallError) refusal = error;
        else throw error;
      }
      if (refusal) {
        expect(refusal.code, sourceId).toBe(refusal.wouldRemoveHops.length > 0
          ? 'source_model_in_route_chain'
          : 'source_last_supplier');
        answer = await store.patchSource(sourceId, {
          base_url,
          force: true,
          would_remove_hops: refusal.wouldRemoveHops,
          would_interrupt: refusal.wouldInterrupt,
        });
      }
      expect(answer, sourceId).toBeDefined();
      const result = answer!;
      const candidateModelIds = new Set(result.source.models
        .filter((model) => model.retired !== true)
        .map((model) => model.id));
      const expectedRemoved = beforeReferences.filter((hop) => !candidateModelIds.has(hop.model_id));
      expect(result.removed_hops, sourceId).toEqual(expectedRemoved);
      expect(refusal?.wouldRemoveHops ?? [], sourceId).toEqual(expectedRemoved);

      const removed = new Set(expectedRemoved.map((hop) =>
        `${hop.backend}\u0000${hop.menu_model}\u0000${hop.source_id}\u0000${hop.model_id}`));
      for (const agent of store.agents) {
        expect(agent.sources?.order ?? [], `${sourceId}:${agent.backend}:order`)
          .toEqual(beforeOrders.get(agent.backend));
        for (const [menuModel, route] of Object.entries(agent.routes ?? {})) {
          const before = beforeRoutes.get(`${agent.backend}\u0000${menuModel}`) ?? [];
          expect(route.hops, `${sourceId}:${agent.backend}:${menuModel}:route`).toEqual(
            before.filter((hop) =>
              !removed.has(`${agent.backend}\u0000${menuModel}\u0000${hop.source_id}\u0000${hop.model_id}`)),
          );
          for (const hop of route.hops.filter((hop) => hop.source_id === sourceId)) {
            expect(candidateModelIds.has(hop.model_id), `${sourceId}:${agent.backend}:${menuModel}:${hop.model_id}`).toBe(true);
          }
        }
      }
    }
  });
});

describe('Model Hub mock replay boundary', () => {
  it('keys a delete by the full state after source reordering', async () => {
    const store = new MockStore();
    const reordered = ['src_anthkey01', 'src_claudepro1', 'src_relay9c1x'];
    await store.putAgentSources('claude', { order: reordered });
    expect((await store.getAgentSources('claude')).sources?.order).toEqual(reordered);

    let refusal: ApiCallError | null = null;
    try {
      await store.deleteSource('src_claudepro1');
    } catch (error) {
      if (error instanceof ApiCallError) refusal = error;
      else throw error;
    }
    expect(refusal?.code).toBe('source_in_route_chain');
    await store.deleteSource('src_claudepro1', {
      force: true,
      would_remove_hops: refusal?.wouldRemoveHops ?? [],
      would_interrupt: refusal?.wouldInterrupt ?? [],
    });
    expect((await store.getAgentSources('claude')).sources?.order)
      .toEqual(['src_anthkey01', 'src_relay9c1x']);
  });

  it('hard-fails every unrecorded policy-bearing entry point with a record command', async () => {
    const calls: Array<[string, (store: MockStore) => Promise<unknown>]> = [
      ['observeApiKeySource', (store) => store.observeApiKeySource({ vendor: 'custom', base_url: 'https://missing.example/v1', key: 'test-only', protocol_order: ['openai_chat', 'openai_responses', 'anthropic'] })],
      ['createApiKeySource', (store) => store.createApiKeySource({ kind: 'api_key', vendor: 'custom', display_name: 'Missing', base_url: 'https://missing.example/v1', supply_channel: 'hub', key: 'test-only', protocol_order: ['openai_chat', 'openai_responses', 'anthropic'] })],
      ['patchSource', (store) => store.patchSource('src_missing0001', { display_name: 'Missing' })],
      ['refreshSource', (store) => store.refreshSource('src_missing0001')],
      ['deleteSource', (store) => store.deleteSource('src_missing0001')],
      ['replaceCredential', (store) => store.replaceCredential('src_missing0001', { key: 'test-only' })],
      ['reauthSource', (store) => store.reauthSource('src_missing0001')],
      ['putAgentSources', (store) => store.putAgentSources('claude', { order: [] })],
      ['putAgentChain', (store) => store.putAgentChain('claude', 'claude-opus-4-6', { hops: [] })],
      ['probeAgent', (store) => store.probeAgent('claude', 'claude-opus-4-6')],
      ['setAgentMode', (store) => store.setAgentMode('claude', 'direct')],
      ['putMenu', (store) => store.putMenu({ view: 'full', checked: [] })],
      ['addCustomModel', (store) => store.addCustomModel('src_missing0001', { id: 'missing-model', display_name: null })],
      ['updateModelReasoningEfforts', (store) => store.updateModelReasoningEfforts('src_missing0001', 'missing-model', ['high'])],
      ['deleteCustomModel', (store) => store.deleteCustomModel('src_missing0001', 'missing-model')],
      ['scanMigration', (store) => store.scanMigration()],
      ['applyMigration', (store) => store.applyMigration(['missing'])],
      ['installRuntime', (store) => store.installRuntime()],
      ['startRuntime', (store) => store.startRuntime()],
      ['startOAuth', (store) => store.startOAuth('anthropic', 'hub')],
      ['getOAuthStatus', (store) => store.getOAuthStatus('flow_missing')],
      ['submitOAuth', (store) => store.submitOAuth('flow_missing', 'test-only')],
      ['cancelOAuth', (store) => store.cancelOAuth('flow_missing')],
    ];
    const nonMutationMembers = new Set([
      'constructor',
      'sources',
      'agents',
      'transitionKey',
      'replay',
      'listSources',
      'listAgents',
      'getAgentSources',
      'getAgentChain',
      'listEvents',
      'getRuntimeStatus',
    ]);
    const reachableMutations = Object.getOwnPropertyNames(MockStore.prototype)
      .filter((name) => !nonMutationMembers.has(name))
      .sort();
    expect(reachableMutations).toEqual(calls.map(([operation]) => operation).sort());

    for (const [operation, call] of calls) {
      let failure: unknown;
      try {
        await call(new MockStore());
      } catch (error) {
        failure = error;
      }
      expect(failure, operation).toBeInstanceOf(UncontractedMockTransitionError);
      const error = failure as UncontractedMockTransitionError;
      expect(error.operation, operation).toBe(operation);
      expect(error.missingKey, operation).toBeTruthy();
      expect(error.generatorCommand, operation).toBe(
        `python3 scripts/generate_model_hub_mock_corpus.py --record-miss ${error.missingKey}`,
      );
      expect(error.message, operation).toContain(error.generatorCommand);
    }
  });
});
