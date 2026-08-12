import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it, vi } from 'vitest';

import { createContinuationSettlement, createSourceCreatedDelivery } from './mutationSettlement';
import type { SourceCreated } from './modelsApi';

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
});
