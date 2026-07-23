import { describe, expect, it } from 'vitest';

import {
  isPlainMemoryCommandRequest,
  memoryCommandResultFromResponse,
  routeWorkbenchMessageResponse,
} from './memoryCommandResult';

describe('ChatPage Memory command response routing', () => {
  it('takes a typed result before queued/message branches so no row is appended', () => {
    const response = routeWorkbenchMessageResponse({
      id: 'must-not-be-appended',
      queued: true,
      memory_command_result: {
        schema_version: 1,
        type: 'memory_command_result',
        command: 'status',
        result: { state: 'ready', pending: 0 },
      },
    });

    expect(response).toEqual({
      kind: 'memory_command_result',
      result: {
        schema_version: 1,
        type: 'memory_command_result',
        command: 'status',
        result: { state: 'ready', pending: 0 },
      },
    });
  });

  it('does not claim foreground state for a text-only direct command', () => {
    expect(isPlainMemoryCommandRequest('/memory status', { hasAttachments: false, hasReferences: false })).toBe(true);
    expect(isPlainMemoryCommandRequest('/memory profile', { hasAttachments: true, hasReferences: false })).toBe(false);
    expect(
      isPlainMemoryCommandRequest('/memory profile', {
        hasAttachments: false,
        hasReferences: false,
        metadata: { quick_reply_for: 'agent-1' },
      }),
    ).toBe(false);
  });

  it('rejects malformed command results so normal responses retain their existing path', () => {
    expect(memoryCommandResultFromResponse({ memory_command_result: { type: 'memory_command_result' } })).toBeNull();
    expect(routeWorkbenchMessageResponse({ id: 'ordinary-message' })).toEqual({
      kind: 'message',
      message: { id: 'ordinary-message' },
    });
  });
});
