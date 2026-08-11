import { describe, expect, it } from 'vitest';

import { modelsSurfaceKind, modelsSurfaceKindFromReads } from './modelHubSurfaceState';
import { failRegionRead, readyRegion } from './regionRead';
import type { AgentSupply, Source } from './types';

const directAgent = (backend: AgentSupply['backend']): AgentSupply => ({
  backend,
  mode: 'direct',
  menu_kind: backend === 'opencode' ? 'open' : 'fixed',
});

const retainedSource = {} as Source;

describe('modelsSurfaceKind', () => {
  it('renders Frame 09 exactly when every backend is direct and no sources remain', () => {
    const agents = [directAgent('claude'), directAgent('codex'), directAgent('opencode')];

    expect(modelsSurfaceKind(agents, [])).toBe('direct_empty');
    expect(modelsSurfaceKind(agents, [retainedSource])).toBe('gateway');
  });

  it('renders Frame 01 as soon as any backend is in Hub mode', () => {
    const agents = [directAgent('claude'), { ...directAgent('codex'), mode: 'hub' as const }];

    expect(modelsSurfaceKind(agents, [])).toBe('gateway');
  });

  it('is derived from current state rather than first-run history', () => {
    const agents = [directAgent('claude')];
    const hubState = [{ ...agents[0], mode: 'hub' as const }];

    expect(modelsSurfaceKind(agents, [])).toBe('direct_empty');
    expect(modelsSurfaceKind(hubState, [])).toBe('gateway');
    expect(modelsSurfaceKind(agents, [])).toBe('direct_empty');
  });

  it.each(['sources', 'agents'] as const)('keeps Frame 09 unreachable while retained %s are degraded', (region) => {
    const agents = [directAgent('claude')];
    const agentsRead = region === 'agents'
      ? failRegionRead(readyRegion(agents))
      : readyRegion(agents);
    const sourcesRead = region === 'sources'
      ? failRegionRead(readyRegion<Source[]>([]))
      : readyRegion<Source[]>([]);

    expect(modelsSurfaceKindFromReads(agentsRead, sourcesRead)).toBe('gateway');
  });
});
