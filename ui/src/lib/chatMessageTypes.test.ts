import { describe, expect, it } from 'vitest';

import {
  isAgentActivityBoundaryMessage,
  isBoundaryMessage,
  isDetachedCompletionMessage,
  isNotifyMessageType,
  isTerminalAgentMessage,
  isTranscriptMessage,
  shouldRefreshAgentActivityForMessage,
} from './chatMessageTypes';
import { messageTypeNames, specFor } from './messageTypes';

describe('isTranscriptMessage', () => {
  it('shows the rows the transcript has always shown, and hides process log', () => {
    for (const type of ['user', 'harness', 'output', 'result', 'notify', 'vault', 'error']) {
      expect(isTranscriptMessage({ type }), type).toBe(true);
    }
    for (const type of ['assistant', 'tool_call', 'draft', 'pending', 'silent', 'future_type']) {
      expect(isTranscriptMessage({ type }), type).toBe(false);
    }
  });

  it('shows an annotation, in either direction', () => {
    expect(isTranscriptMessage({ type: 'annotation' })).toBe(true);
  });

  // The back door this replaced. A forward annotation carries
  // ``metadata.source === 'show_page'`` from the moment it is queued, so the old
  // predicate showed it as a delivered bubble while the very same row was still
  // sitting in the queue strip waiting to be sent. Only the type changes when the
  // flush lands, and the type is now the only thing consulted.
  it('hides a queued annotation however it is dressed', () => {
    const queued = {
      type: 'queued',
      author: 'harness',
      source: 'harness',
      author_name: 'show_annotation',
      metadata: { source: 'show_page', show_event_type: 'human.annotation.created' },
      content: { annotation: { direction: 'user', action: 'created' } },
    };
    expect(isTranscriptMessage(queued)).toBe(false);
  });
});

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
    expect(isTerminalAgentMessage({ author: 'agent', type: 'output' })).toBe(false);
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

  it('never lets a detached legacy result settle the active turn', () => {
    for (const type of ['result', 'error', 'notify']) {
      expect(isTerminalAgentMessage({ author: 'agent', type, metadata: { detached: true } }), type).toBe(false);
    }
  });
});

describe('isBoundaryMessage', () => {
  it('recognizes current output rows and legacy detached results', () => {
    expect(isBoundaryMessage({ type: 'output' })).toBe(true);
    expect(isBoundaryMessage({ type: 'result', metadata: { detached: true } })).toBe(true);
    expect(isBoundaryMessage({ type: 'result' })).toBe(false);
  });

  it('keeps every status-rendered type in its notification family when detached', () => {
    const statusTypes = messageTypeNames().filter((type) => specFor(type).render === 'status');
    expect(statusTypes.length).toBeGreaterThan(0);
    for (const type of statusTypes) {
      expect(isBoundaryMessage({ type, metadata: { detached: true } }), type).toBe(false);
    }
  });
});

describe('isAgentActivityBoundaryMessage', () => {
  it('advances Activity only for the catalog effective boundary role', () => {
    for (const type of messageTypeNames()) {
      const spec = specFor(type);
      for (const detached of [false, true]) {
        for (const event of [undefined, ...spec.terminalWhenEvents]) {
          const message = { type, metadata: { detached, event } };
          const isBoundary = spec.activityRole === 'boundary'
            && !(detached && spec.detachedCompletion)
            && event === undefined;
          expect(isAgentActivityBoundaryMessage({ ...message, author: 'agent' }), type).toBe(isBoundary);
          expect(isAgentActivityBoundaryMessage({ ...message, author: 'user' }), type).toBe(false);
        }
      }
    }
  });
});

describe('isDetachedCompletionMessage', () => {
  it('classifies lifecycle provenance independently from presentation', () => {
    for (const type of ['result', 'error', 'notify']) {
      expect(isDetachedCompletionMessage({ type, metadata: { detached: true } }), type).toBe(true);
    }
    expect(isDetachedCompletionMessage({ type: 'output', metadata: { detached: true } })).toBe(false);
    expect(isDetachedCompletionMessage({ type: 'result' })).toBe(false);
  });
});

describe('shouldRefreshAgentActivityForMessage', () => {
  it('refreshes Activity groups for terminal replies and detached completions', () => {
    for (const type of ['output', 'result', 'error']) {
      expect(shouldRefreshAgentActivityForMessage({ author: 'agent', type }), type).toBe(true);
    }
    for (const type of ['result', 'error', 'notify']) {
      expect(
        shouldRefreshAgentActivityForMessage({ author: 'agent', type, metadata: { detached: true } }),
        type,
      ).toBe(true);
    }
  });

  it('ignores non-boundary process and user rows', () => {
    expect(shouldRefreshAgentActivityForMessage({ author: 'agent', type: 'assistant' })).toBe(false);
    expect(shouldRefreshAgentActivityForMessage({ author: 'user', type: 'result' })).toBe(false);
  });
});
