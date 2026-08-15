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
