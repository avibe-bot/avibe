import { describe, expect, it } from 'vitest';

import { chatRowKind, drawsEmptyBodyPlaceholder, isAgentAuthored } from './chatRowKind';

const row = (over: Partial<Parameters<typeof chatRowKind>[0]>) =>
  chatRowKind({ type: 'user', author: 'user', source: 'user', ...over });

// The row shapes the contract freezes (docs/plans/show-annotation-message-type/
// examples.json). A forward annotation is authored by ``harness``; a reverse mark
// is authored by ``agent``. Both are ``type: 'annotation'``, and that is the only
// thing either of them has in common.
const FORWARD = {
  type: 'annotation',
  author: 'harness',
  source: 'harness',
  content: { annotation: { direction: 'user', action: 'created', quote: 'Model Hub' } },
};
const REVERSE = {
  type: 'annotation',
  author: 'agent',
  source: null,
  content: { annotation: { direction: 'agent', action: 'resolved' } },
};

describe('chatRowKind', () => {
  it('keeps the pre-existing role families intact', () => {
    expect(row({ author: 'user', source: 'user' })).toEqual({ kind: 'user' });
    expect(row({ author: 'agent', type: 'result' })).toEqual({ kind: 'agent' });
    expect(row({ author: 'system', type: 'user' })).toEqual({ kind: 'system' });
    expect(row({ author: 'harness', source: 'harness', type: 'harness' })).toEqual({ kind: 'harness' });
    expect(row({ author: 'agent', type: 'notify' })).toEqual({ kind: 'notify' });
    expect(row({ author: 'agent', type: 'error' })).toEqual({ kind: 'notify' });
  });

  it('draws an annotation card for both directions', () => {
    expect(chatRowKind(FORWARD)).toEqual({
      kind: 'annotation',
      annotation: { direction: 'user', resolved: false, quote: 'Model Hub' },
    });
    expect(chatRowKind(REVERSE)).toEqual({
      kind: 'annotation',
      annotation: { direction: 'agent', resolved: true, quote: undefined },
    });
  });

  // The defect this lane exists to remove. A forward annotation is turn input, so
  // it is authored by ``harness`` — under an author-first cascade it drew as a
  // collapsed trigger row, and the user's own words sat behind a chip.
  it('lets the type outvote the author, never the other way round', () => {
    for (const author of ['harness', 'agent', 'system', 'user']) {
      for (const source of ['harness', 'user', null]) {
        expect(chatRowKind({ ...FORWARD, author, source }).kind, `${author}/${source}`).toBe('annotation');
      }
    }
  });

  // Rule 01: the side is direction, and direction is on the row. Two annotations
  // with the SAME author land on opposite sides; two with the same direction land
  // on the same side whoever authored them.
  it('reads the side from direction, not from who authored the row', () => {
    const asUser = chatRowKind({ ...FORWARD, author: 'agent' });
    const asAgent = chatRowKind({ ...REVERSE, author: 'agent' });
    expect(asUser).toMatchObject({ kind: 'annotation', annotation: { direction: 'user' } });
    expect(asAgent).toMatchObject({ kind: 'annotation', annotation: { direction: 'agent' } });
  });

  // A queued annotation carries the identical content and metadata; only its type
  // says it has not been sent. It belongs to the queue strip (rule 08), so the
  // transcript mapper must not claim it — the old ``metadata.source`` back door
  // did, which is how one row appeared twice.
  it('does not claim a queued annotation for the transcript', () => {
    expect(chatRowKind({ ...FORWARD, type: 'queued' }).kind).not.toBe('annotation');
  });

  // Contract D requires ``content.annotation`` on every row of the type, and the
  // migration backfills it onto the historical reverse marks, so this is a
  // backend defect. It must still degrade inside the family: falling back to the
  // author cascade would draw a mark the AGENT wrote as the user's own bubble.
  it('still draws a card when the display record is missing or malformed', () => {
    for (const content of [undefined, null, {}, { annotation: null }, { annotation: { direction: 'sideways' } }]) {
      expect(chatRowKind({ type: 'annotation', author: 'agent', source: null, content })).toEqual({
        kind: 'annotation',
        annotation: { direction: 'agent', resolved: false },
      });
    }
  });
});

describe('isAgentAuthored', () => {
  // The defect this exists to prevent, stated as the disagreement itself: on a
  // reverse mark the two questions give different answers, and they are supposed
  // to. Deriving authorship from the card family made ``$<NAME>`` in an agent's
  // mark render as inert literal text where the same words in an ordinary reply
  // render an interactive secret-input card.
  it('keeps an agent mark agent-authored even though an annotation card draws it', () => {
    expect(chatRowKind(REVERSE).kind).toBe('annotation');
    expect(isAgentAuthored(REVERSE)).toBe(true);
  });

  // The other direction is the reason this is not simply ``type === 'annotation'``:
  // a forward annotation is the USER's words on an agent-adjacent card, and must
  // never be dressed as something the agent asked for.
  it('never treats the user side of an annotation as agent-authored', () => {
    expect(isAgentAuthored(FORWARD)).toBe(false);
  });

  // Everywhere else it must still agree with the flag it replaced
  // (``!isNotify && author === 'agent'`` on master), so this is a fix confined to
  // annotations rather than a widening of who gets the agent affordances.
  it('matches the pre-annotation rule on every other row', () => {
    expect(isAgentAuthored({ type: 'result', author: 'agent' })).toBe(true);
    expect(isAgentAuthored({ type: 'user', author: 'user' })).toBe(false);
    expect(isAgentAuthored({ type: 'harness', author: 'harness' })).toBe(false);
    expect(isAgentAuthored({ type: 'result', author: 'system' })).toBe(false);
    // A notify/error row draws a status pill with no Markdown body; it was
    // excluded before and stays excluded.
    expect(isAgentAuthored({ type: 'notify', author: 'agent' })).toBe(false);
    expect(isAgentAuthored({ type: 'error', author: 'agent' })).toBe(false);
  });
});

describe('drawsEmptyBodyPlaceholder', () => {
  // An ordinary row really is broken-looking when it is empty, which is why the
  // stand-in exists at all.
  it('fills an ordinary empty bubble', () => {
    expect(drawsEmptyBodyPlaceholder({ kind: 'user' }, false)).toBe(true);
    expect(drawsEmptyBodyPlaceholder({ kind: 'agent' }, false)).toBe(true);
    expect(drawsEmptyBodyPlaceholder({ kind: 'harness' }, false)).toBe(true);
  });

  // The empty annotation is a real, supported shape in BOTH directions — a pure
  // highlight the annotator submitted with no comment (backend: an empty
  // ``comment`` / empty mark ``body``). The frozen contract says the card then
  // renders its title, quote and attachments ALONE, so an em dash here would be
  // a body the annotator never wrote.
  it('leaves an empty annotation to its title and quote', () => {
    for (const direction of ['user', 'agent'] as const) {
      const row = { kind: 'annotation' as const, annotation: { direction, resolved: false } };
      expect(drawsEmptyBodyPlaceholder(row, false)).toBe(false);
    }
  });

  // Unchanged from before the card existed: an attachment already fills the
  // bubble, so nothing stands in for the missing words.
  it('never draws it when an attachment already fills the bubble', () => {
    expect(drawsEmptyBodyPlaceholder({ kind: 'user' }, true)).toBe(false);
    expect(drawsEmptyBodyPlaceholder({ kind: 'agent' }, true)).toBe(false);
  });
});
