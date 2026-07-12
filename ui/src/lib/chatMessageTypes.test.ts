import { describe, expect, it } from 'vitest';

import { isNotifyMessageType, isTerminalAgentMessage } from './chatMessageTypes';

describe('isNotifyMessageType', () => {
  it('renders current and legacy failure rows as notifications', () => {
    expect(isNotifyMessageType('notify')).toBe(true);
    expect(isNotifyMessageType('error')).toBe(true);
  });

  it('keeps agent results out of the notification treatment', () => {
    expect(isNotifyMessageType('result')).toBe(false);
    expect(isNotifyMessageType('assistant')).toBe(false);
  });
});

describe('isTerminalAgentMessage', () => {
  it('recognizes results, legacy errors, and structured backend failures', () => {
    expect(isTerminalAgentMessage({ author: 'agent', type: 'result' })).toBe(true);
    expect(isTerminalAgentMessage({ author: 'agent', type: 'error' })).toBe(true);
    expect(
      isTerminalAgentMessage({
        author: 'agent',
        type: 'notify',
        metadata: { event: 'backend_failure' },
      }),
    ).toBe(true);
  });

  it('keeps ordinary notifications and user rows nonterminal', () => {
    expect(isTerminalAgentMessage({ author: 'agent', type: 'notify' })).toBe(false);
    expect(
      isTerminalAgentMessage({
        author: 'agent',
        type: 'notify',
        metadata: { event: 'activity_completed' },
      }),
    ).toBe(false);
    expect(isTerminalAgentMessage({ author: 'user', type: 'result' })).toBe(false);
  });
});
