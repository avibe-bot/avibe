import { describe, expect, it, vi } from 'vitest';
import type { VibeAgentBrief, VibeAgentFull } from '../../context/ApiContext';
import { isBuiltinAgent, readSetupTargets, setupTargetFor } from './setupTargets';

const agent = (source: string, metadata: Record<string, unknown>) => ({
  name: 'claude', backend: 'claude', enabled: true, archived: false, source, metadata,
} as VibeAgentBrief & { metadata: Record<string, unknown> });

const brief = (overrides: Partial<VibeAgentBrief> & { name: string; backend: VibeAgentBrief['backend'] }): VibeAgentBrief => ({
  id: `${overrides.backend}-${overrides.name}`,
  display_name: overrides.name,
  description: null,
  model: null,
  reasoning_effort: null,
  enabled: true,
  archived: false,
  archived_at: null,
  source: 'file',
  updated_at: '',
  ...overrides,
});

const full = (
  row: VibeAgentBrief,
  metadata: Record<string, unknown>,
  extra: Partial<VibeAgentFull> = {},
): VibeAgentFull => ({
  ...row,
  system_prompt: null,
  created_at: '',
  metadata,
  ...extra,
});

describe('C6 store metadata identity', () => {
  it('does not infer a designated builtin from source provenance alone', () => {
    const row = agent('builtin', {});
    expect(isBuiltinAgent(row)).toBe(false);
    expect(setupTargetFor([row], 'claude')).toBeNull();
  });

  it('recognizes the store builtin_default marker regardless of source provenance', () => {
    const row = agent('file', { builtin_default: true });
    expect(setupTargetFor([row], 'claude')).toBe(row);
  });

  it('recognizes lock_delete as the same store identity', () => {
    const row = agent('user', { lock_delete: true });
    expect(isBuiltinAgent(row)).toBe(true);
    expect(setupTargetFor([row], 'claude')).toBe(row);
  });

  it('prefers the backend-named designated builtin, then default, then name order', () => {
    const alpha = agent('file', { builtin_default: true });
    const named = { ...alpha, name: 'claude' };
    const fallback = { ...alpha, name: 'default' };
    const other = { ...alpha, name: 'aa-builtin' };
    expect(setupTargetFor([other, fallback, named], 'claude')).toBe(named);
    expect(setupTargetFor([other, fallback], 'claude')).toBe(fallback);
    expect(setupTargetFor([other], 'claude')).toBe(other);
  });
});

describe('uncached full-Agent target reads', () => {
  it('queries relevant candidates concurrently with cache:false and keeps the current record', async () => {
    const claude = brief({ name: 'claude', backend: 'claude', model: 'stale-model' });
    const custom = brief({ name: 'mine', backend: 'claude', source: 'user' });
    const current = full(claude, { builtin_default: true }, { model: 'openai/gpt-5.6-sol' });
    const getVibeAgent = vi.fn(async (name: string, params?: { cache?: boolean }) => {
      expect(params).toEqual({ cache: false });
      if (name === 'claude') return { ok: true, agent: current };
      return { ok: true, agent: full(custom, {}) };
    });

    const targets = await readSetupTargets([claude, custom], { getVibeAgent });
    expect(getVibeAgent).toHaveBeenCalledTimes(2);
    expect(getVibeAgent.mock.calls.every(([, params]) => params?.cache === false)).toBe(true);
    expect(targets).toEqual([current]);
    expect(targets[0].model).toBe('openai/gpt-5.6-sol');
  });

  it('does not designate a target when metadata cannot be read', async () => {
    const row = brief({ name: 'claude', backend: 'claude', source: 'builtin' });
    const getVibeAgent = vi.fn(async () => ({ ok: false, agent: null }));
    expect(await readSetupTargets([row], { getVibeAgent })).toEqual([]);
    expect(getVibeAgent).toHaveBeenCalledWith('claude', { cache: false });
  });

  it('does not designate a target when the full read throws or disagrees about identity', async () => {
    const claude = brief({ name: 'claude', backend: 'claude', source: 'builtin' });
    const renamed = brief({ name: 'codex', backend: 'codex', source: 'builtin' });
    const getVibeAgent = vi.fn(async (name: string) => {
      if (name === 'claude') throw new Error('engine_down');
      return { ok: true, agent: full(renamed, { builtin_default: true }, { name: 'someone-else' }) };
    });
    expect(await readSetupTargets([claude, renamed], { getVibeAgent })).toEqual([]);
  });
});
