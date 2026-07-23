import { describe, expect, it } from 'vitest';

import { memoryCommandResultFromResponse } from './memoryCommandResult';

describe('memoryCommandResultFromResponse', () => {
  it('claims a typed result before ordinary message response fields', () => {
    const memoryResult = memoryCommandResultFromResponse({
      id: 'must-not-be-appended',
      queued: true,
      memory_command_result: {
        schema_version: 1,
        type: 'memory_command_result',
        command: 'status',
        result: { state: 'ready', pending: 0 },
      },
    });

    expect(memoryResult).toEqual({
      schema_version: 1,
      type: 'memory_command_result',
      command: 'status',
      result: { state: 'ready', pending: 0 },
    });
  });

  it('rejects malformed command results so normal responses retain their existing path', () => {
    expect(memoryCommandResultFromResponse({ memory_command_result: { type: 'memory_command_result' } })).toBeNull();
    expect(memoryCommandResultFromResponse({ id: 'ordinary-message' })).toBeNull();
  });
});
