import { describe, expect, it } from 'vitest';

import { isNotifyMessageType, isTerminalAgentMessage, isTranscriptMessage } from './chatMessageTypes';

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

describe('isTranscriptMessage (visibility is a function of type alone)', () => {
  it('mirrors the server transcript types and keeps the process log out', () => {
    for (const type of ['user', 'harness', 'result', 'error', 'notify', 'annotation']) {
      expect(isTranscriptMessage({ type })).toBe(true);
    }
    for (const type of ['assistant', 'tool_call', 'pending', 'queued']) {
      expect(isTranscriptMessage({ type })).toBe(false);
    }
  });

  // The exact defect this change removes. The guard used to keep ANY row whose
  // ``metadata.source`` was ``show_page`` regardless of its type — and a forward
  // annotation carries that source in every state, so a still-queued annotation
  // rendered as a delivered bubble while the identical row sat in the queue
  // strip. This is the frozen queued example (examples.json, msg_01J8XK5M8T),
  // ``show_page`` origin and all: it must stay out of the transcript. Widening
  // the guard by origin again turns this red.
  it('hides a queued annotation even though it came from a Show Page', () => {
    const queued = {
      id: 'msg_01J8XK5M8T',
      type: 'queued',
      author: 'harness',
      source: 'harness',
      author_name: 'show_annotation',
      metadata: { source: 'show_page', show_event_id: 'evt_b0c4d5e6' },
    };
    // Cross-assert the fixture's own identity, so a drifting copy cannot make
    // the real assertion below pass vacuously.
    expect(queued.type).toBe('queued');
    expect(queued.metadata.source).toBe('show_page');
    expect(isTranscriptMessage(queued)).toBe(false);
    // ...and the same row appears exactly once the flush mints it. Nothing but
    // ``type`` changed between these two lines.
    expect(isTranscriptMessage({ ...queued, type: 'annotation' })).toBe(true);
  });
});
