import { describe, expect, it, vi } from 'vitest';

import {
  MAX_REMEMBERED_SHOW_PAGE_SESSIONS,
  readChatViewMode,
  writeChatViewMode,
} from './chatViewMemory';

function memoryStorage(initial: string | null = null) {
  let value = initial;
  return {
    storage: {
      getItem: vi.fn(() => value),
      setItem: vi.fn((_key: string, next: string) => {
        value = next;
      }),
    },
    value: () => value,
  };
}

describe('chat view memory', () => {
  it('defaults each session to chat and remembers Show Page independently', () => {
    const memory = memoryStorage();

    expect(readChatViewMode('ses_a', memory.storage)).toBe('chat');
    writeChatViewMode('ses_a', 'show-page', memory.storage);

    expect(readChatViewMode('ses_a', memory.storage)).toBe('show-page');
    expect(readChatViewMode('ses_b', memory.storage)).toBe('chat');
  });

  it('switching back to chat clears only that session preference', () => {
    const memory = memoryStorage();
    writeChatViewMode('ses_a', 'show-page', memory.storage);
    writeChatViewMode('ses_b', 'show-page', memory.storage);
    writeChatViewMode('ses_a', 'chat', memory.storage);

    expect(readChatViewMode('ses_a', memory.storage)).toBe('chat');
    expect(readChatViewMode('ses_b', memory.storage)).toBe('show-page');
  });

  it('bounds and deduplicates remembered sessions', () => {
    const oversized = Array.from(
      { length: MAX_REMEMBERED_SHOW_PAGE_SESSIONS + 2 },
      (_, index) => `ses_${index}`,
    );
    const memory = memoryStorage(JSON.stringify([...oversized, oversized.at(-1)]));

    writeChatViewMode('ses_latest', 'show-page', memory.storage);

    const stored = JSON.parse(memory.value() ?? '[]') as string[];
    expect(stored).toHaveLength(MAX_REMEMBERED_SHOW_PAGE_SESSIONS);
    expect(stored.at(-1)).toBe('ses_latest');
    expect(new Set(stored).size).toBe(stored.length);
  });

  it('tolerates corrupt or unavailable storage', () => {
    const corrupt = memoryStorage('{not json');
    expect(readChatViewMode('ses_a', corrupt.storage)).toBe('chat');
    expect(() => writeChatViewMode('ses_a', 'show-page', corrupt.storage)).not.toThrow();
    expect(readChatViewMode('ses_a', corrupt.storage)).toBe('show-page');

    const blocked = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(readChatViewMode('ses_a', blocked)).toBe('chat');
    expect(() => writeChatViewMode('ses_a', 'show-page', blocked)).not.toThrow();
  });
});
