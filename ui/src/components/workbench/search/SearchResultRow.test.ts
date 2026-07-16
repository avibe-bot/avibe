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
});
