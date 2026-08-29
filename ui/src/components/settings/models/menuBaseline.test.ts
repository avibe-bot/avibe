import { describe, expect, it, vi } from 'vitest';

import {
  applyOpenCodeMenuIntent,
  openCodeMenuIntent,
  openCodeMenuIntentApplied,
  readOpenCodeMenuBaseline,
} from './menuBaseline';
import type { AgentSupply, Source } from './types';

const source = (id: string, modelIds: string[] = []): Source => ({
  id,
  last_discovered_at: null,
  kind: 'api_key',
  vendor: 'openai',
  display_name: id,
  protocol: 'openai_responses',
  supply_channel: 'hub',
  billing: 'metered',
  state: { status: 'active', retry_at: null, detail_key: null },
  models: modelIds.map((modelId) => ({ id: modelId, origin: 'manual', reasoning_efforts: [] })),
});

const agent = (sourceIds: string[]): AgentSupply => ({
  backend: 'opencode',
  cli_present: true,
  mode: 'hub',
  menu_kind: 'open',
  sources: {
    order: sourceIds,
    eligibility: sourceIds.map((sourceId) => ({ source_id: sourceId, eligible: true })),
  },
  menu: { view: 'featured', checked: [] },
  routes: {},
  standard_vendors: ['openai'],
});

describe('readOpenCodeMenuBaseline', () => {
  it('accepts an Agent read bracketed by identical complete Source snapshots', async () => {
    const getAgentSources = vi.fn().mockResolvedValue(agent(['src_a']));
    const stable = [source('src_a', ['model-a'])];
    const readValue = vi.fn().mockResolvedValue(stable);

    const baseline = await readOpenCodeMenuBaseline({ getAgentSources }, { readValue });

    expect(baseline.sources).toEqual(stable);
    expect(getAgentSources).toHaveBeenCalledTimes(1);
    expect(readValue).toHaveBeenCalledTimes(2);
  });

  it('rejects Source model content that changes across the Agent read', async () => {
    const getAgentSources = vi.fn().mockResolvedValue(agent(['src_a']));
    const readValue = vi.fn()
      .mockResolvedValueOnce([source('src_a', ['retired-model'])])
      .mockResolvedValueOnce([source('src_a')]);

    await expect(readOpenCodeMenuBaseline({ getAgentSources }, { readValue })).rejects.toThrow(
      'did not converge',
    );
  });

  it('rejects stable Source snapshots with an Agent composition hole', async () => {
    const getAgentSources = vi.fn().mockResolvedValue(agent(['src_a', 'src_new']));
    const readValue = vi.fn().mockResolvedValue([source('src_a')]);

    await expect(readOpenCodeMenuBaseline({ getAgentSources }, { readValue })).rejects.toThrow(
      'did not converge',
    );
  });
});

describe('OpenCode menu intent', () => {
  it('preserves unrelated current selections and ignores additions no longer selectable', () => {
    const intent = openCodeMenuIntent(
      ['openai/kept', 'openai/removed'],
      ['openai/kept', 'openai/added', 'openai/gone'],
    );

    expect(applyOpenCodeMenuIntent(
      ['openai/concurrent', 'openai/kept', 'openai/removed'],
      intent,
      new Set(['openai/added']),
    )).toEqual(['openai/concurrent', 'openai/kept', 'openai/added']);
  });

  it('recognizes an applied intent while ignoring unrelated concurrent selections', () => {
    const intent = openCodeMenuIntent(['openai/a', 'openai/remove'], ['openai/a', 'openai/add']);
    expect(openCodeMenuIntentApplied(['openai/a', 'openai/concurrent', 'openai/add'], intent)).toBe(true);
    expect(openCodeMenuIntentApplied(['openai/a', 'openai/add', 'openai/remove'], intent)).toBe(false);
    expect(openCodeMenuIntentApplied(['openai/a'], intent)).toBe(false);
  });
});
