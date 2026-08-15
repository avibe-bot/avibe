import { describe, expect, it } from 'vitest';

import { ApiCallError, MockStore } from './modelsApi';
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
