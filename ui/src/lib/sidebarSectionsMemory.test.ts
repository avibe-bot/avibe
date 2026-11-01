import { describe, expect, it, vi } from 'vitest';

import { readSidebarSectionOpen, writeSidebarSectionOpen } from './sidebarSectionsMemory';

const KEY = 'avibe.sidebar.sections.v1';

function memoryStorage(initial: Record<string, string> = {}) {
  const values: Record<string, string> = { ...initial };
  return {
    storage: {
      getItem: vi.fn((key: string) => values[key] ?? null),
      setItem: vi.fn((key: string, next: string) => {
        values[key] = next;
      }),
    },
    values,
  };
}

describe('sidebar sections memory', () => {
  it('records each section independently and keeps entries it does not own', () => {
    const memory = memoryStorage({ [KEY]: JSON.stringify({ bots: false }) });

    writeSidebarSectionOpen('projects', false, memory.storage);
    expect(readSidebarSectionOpen('projects', memory.storage)).toBe(false);
    expect(readSidebarSectionOpen('capabilities', memory.storage)).toBe(true);

    writeSidebarSectionOpen('capabilities', false, memory.storage);
    writeSidebarSectionOpen('projects', true, memory.storage);
    expect(readSidebarSectionOpen('capabilities', memory.storage)).toBe(false);
    expect(readSidebarSectionOpen('projects', memory.storage)).toBe(true);
    // A section a newer build added survives this build's writes.
    expect(JSON.parse(memory.values[KEY])).toEqual({ bots: false, capabilities: false, projects: true });
  });

  it.each([
    ['nothing stored', undefined],
    ['corrupt JSON', '{"projects":'],
    ['a non-object value', '[false]'],
    ['a non-boolean entry', JSON.stringify({ projects: 'no', capabilities: 0 })],
  ])('opens every section when the browser holds %s', (_label, stored) => {
    const memory = memoryStorage(stored === undefined ? {} : { [KEY]: stored });

    expect(readSidebarSectionOpen('projects', memory.storage)).toBe(true);
    expect(readSidebarSectionOpen('capabilities', memory.storage)).toBe(true);
  });

  it('degrades to open and stays silent when storage is unavailable', () => {
    const blocked = {
      getItem: vi.fn(() => {
        throw new Error('blocked');
      }),
      setItem: vi.fn(() => {
        throw new Error('blocked');
      }),
    };

    expect(() => writeSidebarSectionOpen('projects', false, blocked)).not.toThrow();
    expect(readSidebarSectionOpen('projects', blocked)).toBe(true);
  });
});
