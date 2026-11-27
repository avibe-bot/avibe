import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { deriveList, moduleOwners, unwrappedPageImports } from './localeSurfaces';

// Which copy may leave the first screen is decided from the module graph
// (localeSurfaces.ts). The build exercises it on the real graph; these pin the
// rules on small ones, where a wrong answer means a page drawing a raw key.
const UI = join(dirname(fileURLToPath(import.meta.url)), '..');
const at = (path: string) => join(UI, path);
const AGENTS = 'src/components/workbench/AgentsPage.tsx';
const HARNESS = 'src/components/workbench/HarnessPage.tsx';

type Edges = Record<string, { imports?: string[]; dynamic?: string[] }>;
const graphOf = (edges: Edges) => ({
  getModuleInfo: (id: string) => {
    const entry = Object.entries(edges).find(([path]) => at(path) === id)?.[1];
    return entry ? { importedIds: (entry.imports ?? []).map(at), dynamicallyImportedIds: (entry.dynamic ?? []).map(at) } : null;
  },
});
const ownersOf = (edges: Edges) => {
  const owners = moduleOwners(graphOf(edges), at('src/main.tsx'));
  return (path: string) => [...(owners.get(at(path)) ?? [])].sort();
};

describe('which copy each module can run with', () => {
  it('gives a page its own surface when its loader loads it, and a module two pages import both', () => {
    const owners = ownersOf({
      'src/main.tsx': { imports: ['src/routePages.ts'] },
      'src/routePages.ts': { dynamic: [AGENTS, HARNESS] },
      [AGENTS]: { imports: ['src/agentsOnly.ts', 'src/shared.ts'] },
      [HARNESS]: { imports: ['src/shared.ts'] },
    });

    expect(owners('src/routePages.ts')).toEqual(['first']);
    expect(owners('src/agentsOnly.ts')).toEqual(['agents']);
    expect(owners('src/shared.ts')).toEqual(['agents', 'harness']);
  });

  it('runs a page with the first screen\'s copy when any module but a loader imports it', () => {
    const owners = ownersOf({
      'src/main.tsx': { imports: ['src/routePages.ts', 'src/elsewhere.ts'] },
      'src/routePages.ts': { dynamic: [AGENTS] },
      'src/elsewhere.ts': { dynamic: [AGENTS] },
      [AGENTS]: { imports: ['src/agentsOnly.ts'] },
    });

    expect(owners('src/agentsOnly.ts')).toEqual(['agents', 'first']);
  });
});

describe('the copy each surface holds', () => {
  const copy = {
    agents: { title: 'A', list: { shared: 'B', unnamed: 'C' }, count_one: 'D', count_other: 'E' },
    harness: { title: 'F' },
    chat: { title: 'G' },
  };

  it('moves only copy every name of which runs with one surface', () => {
    const list = deriveList(copy, new Map([
      ['agents.title', new Set(['agents'])],
      ['agents.list.shared', new Set(['agents', 'harness'])],
      ['agents.count', new Set(['agents'])],
      ['harness.title', new Set(['first'])],
      ['chat.title', new Set(['agents'])],
    ]));

    // `agents.list.unnamed` stays: no module names it, so code may build it at run time.
    // `chat` is not a routed page's copy.
    expect(list).toEqual({ settings: [], agents: ['agents.title', 'agents.count_one', 'agents.count_other'], skills: [], harness: [], setup: [] });
  });

  it('keeps a subtree some module takes whole in one piece', () => {
    // A module reads `agents.list` whole (`returnObjects`), so none of it may move
    // while the first screen draws part of it.
    const list = deriveList(copy, new Map([
      ['agents.list', new Set(['agents'])],
      ['agents.list.shared', new Set(['first'])],
      ['agents.list.unnamed', new Set(['agents'])],
    ]));

    expect(list.agents).toEqual([]);
  });
});

describe('a loader', () => {
  const resolve = (specifier: string) => ({ './agents': at(AGENTS), './harness': at(HARNESS) })[specifier];

  it('loads each page with its own surface\'s copy', () => {
    expect(unwrappedPageImports("withCopy('agents', () => import('./agents')); import('./other');", resolve)).toEqual([]);
  });

  it('fails a page loaded without its copy, or with another surface\'s, even beside a wrapped one', () => {
    const code = "withCopy('agents', () => import('./agents')); preload(() => import('./agents')); withCopy('agents', () => import('./harness'));";

    expect(unwrappedPageImports(code, resolve)).toEqual([['agents', './agents'], ['harness', './harness']]);
  });
});
