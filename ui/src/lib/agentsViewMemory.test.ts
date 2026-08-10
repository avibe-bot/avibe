import { describe, expect, it, vi } from 'vitest';

import {
  AGENTS_TAB_ORDER,
  DEFAULT_AGENTS_TAB,
  DEFAULT_AGENT_GRAPH_MODE,
  readAgentGraphMode,
  readAgentsTab,
  writeAgentGraphMode,
  writeAgentsTab,
} from './agentsViewMemory';

function memoryStorage(initial: Record<string, string> = {}) {
  const values: Record<string, string> = { ...initial };
  return {
    storage: {
      getItem: vi.fn((key: string) => values[key] ?? null),
      setItem: vi.fn((key: string, next: string) => {
        values[key] = next;
      }),
    },
    values: () => values,
  };
}

describe('agents view memory', () => {
  it('defaults to Definitions and 含历史 on a fresh browser', () => {
    const memory = memoryStorage();

    expect(readAgentsTab(memory.storage)).toBe(DEFAULT_AGENTS_TAB);
    expect(readAgentsTab(memory.storage)).toBe('definitions');
    expect(readAgentGraphMode(memory.storage)).toBe(DEFAULT_AGENT_GRAPH_MODE);
    expect(readAgentGraphMode(memory.storage)).toBe('history');
  });

  it('resumes the remembered tab and graph mode independently', () => {
    const memory = memoryStorage();

    writeAgentsTab('running', memory.storage);
    expect(readAgentsTab(memory.storage)).toBe('running');
    // Remembering Runs must not disturb the mode inside it.
    expect(readAgentGraphMode(memory.storage)).toBe('history');

    writeAgentGraphMode('active', memory.storage);
    expect(readAgentGraphMode(memory.storage)).toBe('active');
    expect(readAgentsTab(memory.storage)).toBe('running');

    writeAgentsTab('definitions', memory.storage);
    expect(readAgentsTab(memory.storage)).toBe('definitions');
    expect(readAgentGraphMode(memory.storage)).toBe('active');
  });

  it('every tab in the rendered order round-trips', () => {
    const memory = memoryStorage();
    for (const tab of AGENTS_TAB_ORDER) {
      writeAgentsTab(tab, memory.storage);
      expect(readAgentsTab(memory.storage)).toBe(tab);
    }
  });

  it('opens the default when the stored value is not a tab or mode', () => {
    // A tab removed in a later build, or a hand-edited value, must not select
    // nothing — it lands on the default.
    const stale = memoryStorage({
      'avibe.agents.tab.v1': 'webhooks',
      'avibe.agents.graph-mode.v1': 'queued',
    });

    expect(readAgentsTab(stale.storage)).toBe('definitions');
    expect(readAgentGraphMode(stale.storage)).toBe('history');
  });

  it('tolerates blocked storage', () => {
    const blocked = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };

    expect(readAgentsTab(blocked)).toBe('definitions');
    expect(readAgentGraphMode(blocked)).toBe('history');
    expect(() => writeAgentsTab('running', blocked)).not.toThrow();
    expect(() => writeAgentGraphMode('active', blocked)).not.toThrow();
  });
});
