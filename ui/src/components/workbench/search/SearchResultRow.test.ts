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

  // ``annotation`` is the first type that carries both directions, and the rows
  // below are the frozen contract shapes: a forward annotation — the user's own
  // words — is harness-authored because it enters as turn input, and a reverse
  // mark is agent-authored with no source. Neither is automated: nobody
  // scheduled them, so the harness author must not be read as one here.
  it('reads an annotation as its direction, not as a harness-authored row', () => {
    expect(messageSearchRole({ author: 'harness', source: 'harness', type: 'annotation' })).toBe(
      'you',
    );
    expect(messageSearchRole({ author: 'agent', source: null, type: 'annotation' })).toBe('agent');
  });

  // The same author/source pair on any other type is still automated, so the
  // annotation case is a property of that type and not a hole in the rule.
  it('keeps a harness-authored row of any other type automated', () => {
    expect(messageSearchRole({ author: 'harness', source: 'harness', type: 'harness' })).toBe(
      'automated',
    );
    expect(messageSearchRole({ author: 'harness', source: 'harness', type: 'result' })).toBe(
      'automated',
    );
  });

  // The catalog still decides a row whose author names nobody the mapping knows,
  // so a future harness-input type inherits the automated treatment for free.
  it('falls back to the catalog input-turn identity for an unknown author', () => {
    expect(messageSearchRole({ author: 'system', source: null, type: 'harness' })).toBe('automated');
    expect(messageSearchRole({ author: 'system', source: null, type: 'result' })).toBe('agent');
  });
});
