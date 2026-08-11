import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

import { convergeMutation, createIntentAuthority } from './mutationConvergence';

describe('convergeMutation', () => {
  it('gives a newly injected mutation entity and intent before reconciliation', async () => {
    const order: string[] = [];
    let finishRead: (() => void) | undefined;
    const reconcile = vi.fn(() => new Promise<void>((resolve) => { finishRead = resolve; }));
    const authority = createIntentAuthority();

    const pending = convergeMutation({
      entity: { id: 'new-row' },
      applyEntity: ({ id }) => order.push(`entity:${id}`),
      intent: { authority, apply: () => order.push('intent:new-row') },
      reconcile,
    });

    expect(order).toEqual(['entity:new-row', 'intent:new-row']);
    expect(reconcile).toHaveBeenCalledOnce();
    authority.commit(() => order.push('intent:user-newer'));
    finishRead?.();

    await expect(pending).resolves.toBe('superseded');
    expect(order.at(-1)).toBe('intent:user-newer');
  });

  it('applies a reconciled entity even when no immediate UI intent exists', async () => {
    const apply = vi.fn();
    await expect(convergeMutation({
      entity: { id: 'reconciled-row' },
      applyEntity: apply,
      reconcile: vi.fn().mockResolvedValue(undefined),
    })).resolves.toBe('current');
    expect(apply).toHaveBeenCalledWith({ id: 'reconciled-row' });
  });

  it('owns active source and supply mutation convergence at the page boundary', () => {
    const page = readFileSync(join(__dirname, 'SettingsModelsPage.tsx'), 'utf8');
    const detail = readFileSync(join(__dirname, 'SourceDetailPanel.tsx'), 'utf8');
    const mutationOwners = [...page.matchAll(/const (\w+) = React\.useCallback\(async \([^)]*\) => \{\n\s+await convergeMutation\(\{/g)]
      .map((match) => match[1]);

    expect(mutationOwners).toEqual(expect.arrayContaining(['agentSaved', 'sourceMutation']));
    expect(page).toMatch(/const sourceAdded = async[\s\S]*?await convergeMutation\(\{[\s\S]*?intent:\s*\{[\s\S]*?reconcile: refresh/);
    expect(detail).toMatch(/reconcileRemoval[\s\S]*?await onMutation\(reconciliation\.value\)/);
    expect(detail).not.toMatch(/onSourceEcho|onChanged/);
  });
});
