import { readFileSync } from 'node:fs';

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
    for (const type of ['user', 'harness', 'result', 'error', 'notify', 'status', 'annotation']) {
      expect(isTranscriptMessage({ type })).toBe(true);
    }
    for (const type of ['assistant', 'tool_call', 'pending', 'queued']) {
      expect(isTranscriptMessage({ type })).toBe(false);
    }
  });

  // Both halves together, because the pair IS the definition of ``status``: it
  // exists because the types a Show page update and a Show runtime error used to
  // borrow — ``notify`` and ``error`` — also mean "the turn ended", so a page
  // update cleared the awaiting state and a runtime hiccup marked the turn
  // failed. ``isTerminalAgentMessage`` is where the UI makes that same call, so
  // adding ``status`` to it would rebuild the bug on this side of the wire.
  it('shows a Show status row without ending the turn', () => {
    expect(isTranscriptMessage({ type: 'status' })).toBe(true);
    expect(isTerminalAgentMessage({ author: 'agent', type: 'status' })).toBe(false);
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

// The guard is only worth as much as its coverage of the entry points. Chat
// seeds its transcript from several payloads — the initial bootstrap, the tail
// refresh, the older page, the reconnect window, the deep-link window — and one
// of them shipping unfiltered is exactly the defect this change removes: the
// bootstrap endpoint still widens its selection by ``metadata.source``, so an
// unfiltered load put a queued annotation into the transcript AND the queue
// strip while the live path correctly hid it (Codex P1).
//
// A render test can't reach this (Chat needs a DOM the suite doesn't have), and
// a unit test of the predicate passes whether or not anyone calls it. So the
// property is asserted where it actually lives: in the source. Every ``messages``
// array read off an API response in ChatPage must be narrowed on the spot.
describe('transcript entry points (every payload passes the guard)', () => {
  it('narrows every API message payload in ChatPage with isTranscriptMessage', () => {
    const source = readFileSync(new URL('../components/workbench/ChatPage.tsx', import.meta.url), 'utf8');
    const reads = [...source.matchAll(/\.messages\b/g)];
    // Not vacuous: the entry points are the five listed above.
    expect(reads.length).toBeGreaterThanOrEqual(5);

    const unfiltered = reads
      .map((match) => {
        const rest = source.slice(match.index + match[0].length).replace(/\s+/g, '');
        const line = source.slice(0, match.index).split('\n').length;
        return rest.startsWith('.filter(isTranscriptMessage)') ? null : `ChatPage.tsx:${line}`;
      })
      .filter(Boolean);
    expect(unfiltered).toEqual([]);
  });
});
