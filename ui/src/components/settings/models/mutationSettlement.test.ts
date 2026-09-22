import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it, vi } from 'vitest';

import en from '../../../i18n/en.json';
import zh from '../../../i18n/zh.json';
import { FIRST_PAINT_REGION_WHITELIST } from './firstPaintRegions';
import { modelChainKey, type ModelChainRequest } from './modelRows';
import type { SourceCreated } from './modelsApi';
import {
  createContinuationSettlement,
  createSourceCreatedDelivery,
  readSurfaceLanding,
  sourceMutationReadScope,
  SOURCE_MUTATION_OUTCOMES,
  SOURCE_MUTATION_TOAST,
} from './mutationSettlement';
import { readyRegion } from './regionRead';
import type { AgentChain, AgentSupply, RuntimeDependency, Source } from './types';

const translated = (bundle: unknown, key: string): unknown =>
  key.split('.').reduce<unknown>((node, part) => {
    if (!node || typeof node !== 'object') return undefined;
    return (node as Record<string, unknown>)[part];
  }, bundle);

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

  it('reads the whole first-paint surface plus exactly the chains the impact named', async () => {
    const impact = {
      hops: [{ backend: 'claude' as const, menu_model: 'claude-opus-4-6', position: 1, source_id: 'src', model_id: 'model-a' }],
      gaps: [{ backend: 'codex' as const, model_id: 'gpt-5.6-sol', agents: ['release'] }],
    };
    const affectedChains = sourceMutationReadScope(impact).affectedChains;
    const calls: string[] = [];
    let requested: readonly ModelChainRequest[] = [];
    const reads = await readSurfaceLanding({
      sources: async () => { calls.push('sources'); return [] as Source[]; },
      supply: async () => { calls.push('supply'); return [] as AgentSupply[]; },
      runtime: async () => { calls.push('runtime'); return {} as RuntimeDependency; },
      chains: async (requests) => {
        calls.push('chains');
        requested = requests;
        return Object.fromEntries(requests.map(({ backend, modelId }) => [
          modelChainKey(backend, modelId),
          readyRegion({} as AgentChain),
        ]));
      },
    }, affectedChains);

    // A region that draws the surface must be read by the mutation that changes
    // it, so the whitelist — not a second list kept here — names the coverage.
    const expected = new Set([...Object.keys(FIRST_PAINT_REGION_WHITELIST), 'chains']);
    expect(new Set(calls)).toEqual(expected);
    expect(new Set(Object.keys(reads))).toEqual(expected);

    // The impact evidence is the whole chain scope: no route the write did not
    // touch is refetched, and every one it did touch is.
    expect(requested).toEqual(affectedChains);
    expect(affectedChains).toHaveLength(2);
    expect(reads.chains.kind).toBe('ready');
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
    // The page-wide refresh toast answers for the first-paint surface only. A
    // route chain it could not read is stale where it is drawn, with its own
    // Retry, and must not raise an alarm about the whole page.
    expect(refresh).toContain('firstPaintFailed(landing)');
    expect(refresh).toContain("t('settings.models.toast.refreshFailed')");
    expect(settlement).toMatch(/Promise<SourceMutationLanding>/);
    expect(settlement).toContain('return refresh(affectedChains)');
    expect((detail.match(/dispatchManageStage\(\{ type: 'settled' \}\)/g) ?? []).length).toBe(1);
    expect(committed).toMatch(/await onMutationCommitted[\s\S]*dispatchManageStage\(\{ type: 'settled' \}\)/);
  });
});

/**
 * The commit envelope is the only thing the page sees of a write, so the word it
 * carries is the word the user reads. The finding this block exists for: an edit
 * whose Source turned out to be absent still announced 「供应商已更新」 over a panel
 * saying the provider is no longer there.
 */
describe('committed mutation outcomes', () => {
  it('answers for every outcome a commit can carry', () => {
    expect(Object.keys(SOURCE_MUTATION_TOAST).sort()).toEqual([...SOURCE_MUTATION_OUTCOMES].sort());
  });

  it.each(Object.entries(SOURCE_MUTATION_TOAST))('has copy in both locales for %s', (_outcome, toast) => {
    for (const bundle of [en, zh]) expect(typeof translated(bundle, toast.key)).toBe('string');
  });

  it('never celebrates an outcome that is not the one the user asked for', () => {
    // Tone tracks the outcome, not the branch that renders it: `gone` reaches the
    // page from the EDIT flow, and green over 「已经不在了」 is the contradiction.
    expect(SOURCE_MUTATION_TOAST.updated.tone).toBe('success');
    expect(SOURCE_MUTATION_TOAST.removed.tone).toBe('success');
    expect(SOURCE_MUTATION_TOAST.gone.tone).toBe('warning');
  });

  it('says the same thing as the surface under it when a Source is gone', () => {
    // One fact, one sentence: the toast reuses the detail surface's own copy
    // rather than adding a second wording for the same state.
    expect(SOURCE_MUTATION_TOAST.gone.key).toBe('settings.models.sourceDetail.gone');
  });
});
