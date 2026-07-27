import { describe, expect, it } from 'vitest';

import { messageSearchRole } from './messageSearchRole';

describe('messageSearchRole', () => {
  it('keeps harness-originated matches distinct from human and agent messages', () => {
    expect(messageSearchRole({ author: 'harness', source: 'harness', type: 'harness' })).toBe(
      'automated',
    );
    expect(messageSearchRole({ author: 'user', source: 'harness', type: 'user' })).toBe(
      'automated',
    );
    expect(messageSearchRole({ author: 'user', source: 'user', type: 'user' })).toBe('you');
    expect(messageSearchRole({ author: 'agent', source: 'agent', type: 'result' })).toBe('agent');
  });

  // ``annotation`` is the first type that carries both directions, so it is the
  // first type whose ``inputAuthors`` cannot stand in for who wrote the row.
  // Rows are the frozen contract shapes: forward is harness-authored, reverse is
  // agent-authored with no source.
  it('attributes an annotation by its author, not by the type accepting harness input', () => {
    expect(
      messageSearchRole({ author: 'harness', source: 'harness', type: 'annotation' }),
    ).toBe('automated');
    expect(messageSearchRole({ author: 'agent', source: null, type: 'annotation' })).toBe('agent');
  });

  // The catalog still decides a row whose author names nobody the mapping knows,
  // so a future harness-input type inherits the automated treatment for free.
  it('falls back to the catalog input-turn identity for an unknown author', () => {
    expect(messageSearchRole({ author: 'system', source: null, type: 'harness' })).toBe('automated');
    expect(messageSearchRole({ author: 'system', source: null, type: 'result' })).toBe('agent');
  });
});
