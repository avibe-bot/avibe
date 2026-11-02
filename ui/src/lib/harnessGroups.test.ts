import { describe, expect, it } from 'vitest';

import { groupHarnessRuns, stableHarnessGroupKeys, type TranscriptItem } from './harnessGroups';

type Row = { id: string; type: string; author: string; source?: string | null; content?: unknown; metadata?: Record<string, unknown> | null };

const harness = (id: string): Row => ({ id, type: 'harness', author: 'harness', source: 'harness' });
const user = (id: string): Row => ({ id, type: 'user', author: 'user', source: 'user' });
const reply = (id: string): Row => ({ id, type: 'result', author: 'agent', source: 'agent' });

// The shape a reader sees: a bare id for a lone row, an array for a group.
const shape = (items: TranscriptItem<Row>[]) =>
  items.map((item) => (item.kind === 'message' ? item.message.id : item.members.map(({ message }) => message.id)));

describe('groupHarnessRuns', () => {
  it('folds runs of two or more Harness rows and leaves single ones alone', () => {
    const messages = [user('u1'), harness('h1'), harness('h2'), reply('r1'), harness('h3'), user('u2'), harness('h4'), harness('h5'), harness('h6')];

    const items = groupHarnessRuns(messages, { hasOlder: true });

    expect(shape(items)).toEqual(['u1', ['h1', 'h2'], 'r1', 'h3', 'u2', ['h4', 'h5', 'h6']]);
    // Positions in the loaded list survive grouping; Retry eligibility reads them.
    expect(items[1]).toMatchObject({ members: [{ index: 1 }, { index: 2 }] });
    expect(items[5]).toMatchObject({ members: [{ index: 6 }, { index: 7 }, { index: 8 }] });
  });

  it.each<[string, Row]>([
    ['the user', user('x')],
    ['an Agent reply', reply('x')],
    ['mid-Turn Agent output', { id: 'x', type: 'output', author: 'agent', source: 'agent' }],
    ['muted interim narration', { id: 'x', type: 'interim', author: 'agent', source: 'agent' }],
    ['a status or failure notice', { id: 'x', type: 'notify', author: 'agent', source: 'agent', metadata: { event: 'backend_failure' } }],
    ['an error row', { id: 'x', type: 'error', author: 'agent', source: 'agent' }],
    // A forward annotation is authored by ``harness`` and still is not Harness input.
    ['an annotation card', { id: 'x', type: 'annotation', author: 'harness', source: 'harness', content: { annotation: { direction: 'user' } } }],
  ])('ends a run at %s', (_label, breaker) => {
    const items = groupHarnessRuns([harness('h1'), harness('h2'), breaker, harness('h3'), harness('h4')], { hasOlder: true });

    expect(shape(items)).toEqual([['h1', 'h2'], 'x', ['h3', 'h4']]);
  });

  it('counts a Vault callback as Harness input', () => {
    const vault: Row = { id: 'v1', type: 'vault', author: 'harness', source: 'harness', metadata: { source_actor: 'vault:req_1' } };

    expect(shape(groupHarnessRuns([harness('h1'), vault], { hasOlder: true }))).toEqual([['h1', 'v1']]);
  });

  it('draws no Harness row at all when Harness is hidden, the first included', () => {
    const messages = [harness('h0'), reply('r1'), harness('h1'), harness('h2'), reply('r2'), user('u1')];

    const items = groupHarnessRuns(messages, { hasOlder: false, hideHarness: true });

    expect(shape(items)).toEqual(['r1', 'r2', 'u1']);
    // The replies keep their places in the loaded list.
    expect(items.map((item) => (item.kind === 'message' ? item.index : -1))).toEqual([1, 4, 5]);
  });

  it("keeps the Session's first message out of groups, once it is known to be first", () => {
    const messages = [harness('brief'), harness('h1'), harness('h2')];

    expect(shape(groupHarnessRuns(messages, { hasOlder: false }))).toEqual(['brief', ['h1', 'h2']]);
    // With older pages still unloaded, the first loaded row is not the Session's first.
    expect(shape(groupHarnessRuns(messages, { hasOlder: true }))).toEqual([['brief', 'h1', 'h2']]);
    // A brief followed by a single Harness row leaves nothing to fold.
    expect(shape(groupHarnessRuns(messages.slice(0, 2), { hasOlder: false }))).toEqual(['brief', 'h1']);
  });
});

describe('stableHarnessGroupKeys', () => {
  const keysFor = (messages: Row[], previous: ReadonlyMap<string, string> = new Map(), hasOlder = true) =>
    stableHarnessGroupKeys(groupHarnessRuns(messages, { hasOlder }), previous);

  it('keeps a group key while the group grows live and when older members load above it', () => {
    const first = keysFor([harness('h3'), harness('h4')]);
    const key = first.get('h3');

    const grownLive = keysFor([harness('h3'), harness('h4'), harness('h5')], first);
    expect(grownLive.get('h5')).toBe(key);

    const olderLoaded = keysFor([harness('h1'), harness('h2'), harness('h3'), harness('h4'), harness('h5')], grownLive);
    expect(new Set(olderLoaded.values())).toEqual(new Set([key]));
  });

  it('never hands one key to two groups when a run splits', () => {
    const before = keysFor([harness('h1'), harness('h2'), harness('h3'), harness('h4')]);
    const key = before.get('h1');

    const after = keysFor([harness('h1'), harness('h2'), reply('r'), harness('h3'), harness('h4')], before);

    expect(after.get('h1')).toBe(key);
    expect(after.get('h2')).toBe(key);
    expect(after.get('h3')).not.toBe(key);
    expect(after.get('h4')).toBe(after.get('h3'));
  });
});
