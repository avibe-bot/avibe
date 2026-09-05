import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it, vi } from 'vitest';

import { modelChainKey, type ModelChainIndex } from './modelRows';
import type { SourceCreated } from './modelsApi';
import {
  createContinuationSettlement,
  createSourceCreatedDelivery,
  readSurfaceLanding,
  SOURCE_MUTATION_REPORT_PROJECTIONS,
  sourceMutationLanding,
  sourceMutationReadScope,
  type SourceMutationLandingReads,
} from './mutationSettlement';
import { failRegionRead, readyRegion, unreadRegion } from './regionRead';
import type { AgentChain, AgentSupply, RuntimeDependency, Source } from './types';

// Fails exactly one projection, keyed generically so each key carries its own
// value type into `failRegionRead`. Indexing the reads with a union key hands
// the compiler four candidate types for one inference site and it picks one.
const degradeProjection = <K extends keyof SourceMutationLandingReads>(
  reads: SourceMutationLandingReads,
  projection: K,
): SourceMutationLandingReads => ({ ...reads, [projection]: failRegionRead(reads[projection]) });

describe('mutation settlement fences', () => {
  it('atomically rejects every effect belonging to an invalidated attempt', () => {
    const authority = createContinuationSettlement();
    const attempt = authority.begin();
    const apply = vi.fn();
    authority.invalidate();

    expect(authority.settle(attempt, apply)).toBe('stale');
    expect(apply).not.toHaveBeenCalled();
  });

  it('keeps Source-created delivery behind the same attempt fence', () => {
    const authority = createContinuationSettlement();
    const delivery = createSourceCreatedDelivery();
    const onAdded = vi.fn();
    const onClose = vi.fn();
    const created = { source: { id: 'src_example00' }, added_to: [], adopted_by: [] } as unknown as SourceCreated;
    delivery.update(onAdded, onClose);
    const attempt = authority.begin();
    authority.invalidate();

    expect(delivery.settle(authority, attempt, created)).toBe('stale');
    expect(onAdded).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('keeps child components from bypassing the settlement owners', () => {
    const here = dirname(fileURLToPath(import.meta.url));
    const addDialog = readFileSync(resolve(here, 'AddApiKeyDialog.tsx'), 'utf8');
    const detail = readFileSync(resolve(here, 'SourceDetailPanel.tsx'), 'utf8');
    expect(addDialog).not.toMatch(/onAddedRef|onCloseRef/);
    expect(addDialog).toContain('createdDelivery.settle');
    expect(detail).not.toMatch(/\bonMutation\b|\bonGone\b|beginSourceSnapshot/);
    expect(detail).toContain('settlement.source');
    expect(detail).toContain('settlement.gone');
  });

  it('lands only after every projection referenced by the report was read', async () => {
    const impact = {
      hops: [{ backend: 'claude' as const, menu_model: 'claude-opus-4-6', position: 1, source_id: 'src', model_id: 'model-a' }],
      gaps: [{ backend: 'codex' as const, model_id: 'gpt-5.6-sol', agents: ['release'] }],
    };
    const affectedChains = sourceMutationReadScope(impact).affectedChains;
    const calls: string[] = [];
    const reads = await readSurfaceLanding({
      sources: async () => { calls.push('sources'); return [] as Source[]; },
      supply: async () => { calls.push('supply'); return [] as AgentSupply[]; },
      runtime: async () => { calls.push('runtime'); return {} as RuntimeDependency; },
      chains: async (requests) => {
        calls.push('chains');
        return Object.fromEntries(requests.map(({ backend, modelId }) => [
          modelChainKey(backend, modelId),
          readyRegion({} as AgentChain),
        ]));
      },
    }, affectedChains);

    expect(new Set(calls)).toEqual(new Set(Object.keys(SOURCE_MUTATION_REPORT_PROJECTIONS)));
    expect(new Set(Object.keys(reads))).toEqual(new Set(Object.keys(SOURCE_MUTATION_REPORT_PROJECTIONS)));
    expect(sourceMutationLanding(reads, affectedChains, true).verdict).toBe('landed');

    for (const projection of Object.keys(
      SOURCE_MUTATION_REPORT_PROJECTIONS,
    ) as (keyof typeof SOURCE_MUTATION_REPORT_PROJECTIONS)[]) {
      expect(
        sourceMutationLanding(degradeProjection(reads, projection), affectedChains, true).verdict,
        projection,
      ).toBe('degraded');
    }

    for (const request of affectedChains) {
      const key = modelChainKey(request.backend, request.modelId);
      const missing = readyRegion<ModelChainIndex>({
        ...Object.fromEntries(affectedChains.map(({ backend, modelId }) => [
          modelChainKey(backend, modelId),
          readyRegion({} as AgentChain),
        ] as const)),
        [key]: unreadRegion<AgentChain>(),
      });
      expect(sourceMutationLanding({ ...reads, chains: missing }, affectedChains, true).verdict, key)
        .toBe('degraded');
    }
  });

  it('routes every management settlement through one post-await announcement', () => {
    const here = dirname(fileURLToPath(import.meta.url));
    const page = readFileSync(resolve(here, 'SettingsModelsPage.tsx'), 'utf8');
    const detail = readFileSync(resolve(here, 'SourceDetailPanel.tsx'), 'utf8');
    const refresh = page.slice(page.indexOf('const refresh = React.useCallback'), page.indexOf('\n\n  const trackSourceMutation'));
    const settlement = page.slice(page.indexOf('const trackSourceMutation'), page.indexOf('\n\n  React.useEffect', page.indexOf('const trackSourceMutation')));
    const committed = detail.slice(
      detail.indexOf('const commitManagementMutation'),
      detail.indexOf('\n  const reconcileEditWrite'),
    );

    expect(refresh).toMatch(/Promise<SourceMutationLanding>/);
    expect(refresh).toContain('sourceMutationLanding(');
    expect(settlement).toMatch(/Promise<SourceMutationLanding>/);
    expect(settlement).toContain('return refresh(affectedChains)');
    expect((detail.match(/dispatchManageStage\(\{ type: 'settled' \}\)/g) ?? []).length).toBe(1);
    expect(committed).toMatch(/await onMutationCommitted[\s\S]*dispatchManageStage\(\{ type: 'settled' \}\)/);
  });
});
