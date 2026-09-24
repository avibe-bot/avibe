import { describe, expect, it } from 'vitest';

import { linkifyMentions, type MentionReference } from './mentions';

const REFERENCES: MentionReference[] = [
  { kind: 'agent', name: 'claude' },
  { kind: 'session', session_id: 'ses6jr7c5h2q6', title: 'Bridge' },
];

/**
 * Check that the pass's edits describe the rewrite it performed.
 *
 * A citation's ranges cross this pass on the strength of these numbers alone
 * (see lib/citations), so "close enough" is not a thing they can be. Each edit
 * names a range of the INPUT and how many units replaced it; walking them in
 * order therefore reconstructs which parts of the output are untouched input,
 * and those parts have to be exactly the input's own.
 */
const verifyEdits = (input: string, output: string, edits: readonly {
  start: number; end: number; inserted: number;
}[]) => {
  let read = 0;
  let write = 0;
  const replaced: Array<[string, string]> = [];
  for (const edit of edits) {
    expect(edit.start).toBeGreaterThanOrEqual(read);
    expect(edit.end).toBeGreaterThanOrEqual(edit.start);
    // Everything since the previous edit was carried over verbatim.
    expect(output.slice(write, write + (edit.start - read)))
      .toBe(input.slice(read, edit.start));
    write += edit.start - read;
    replaced.push([input.slice(edit.start, edit.end), output.slice(write, write + edit.inserted)]);
    read = edit.end;
    write += edit.inserted;
  }
  expect(output.slice(write)).toBe(input.slice(read));
  expect(output.length).toBe(write + (input.length - read));
  return replaced;
};

describe('linkifyMentions', () => {
  it('reports every marker it rewrote, in the coordinates it was handed', () => {
    const input = 'Ask @<claude> about #<ses6jr7c5h2q6>, then @<claude> again.';
    const { text, edits } = linkifyMentions(input, REFERENCES);

    const replaced = verifyEdits(input, text, edits);
    expect(replaced.map(([from]) => from))
      .toEqual(['@<claude>', '#<ses6jr7c5h2q6>', '@<claude>']);
    // The session chip shows the title rather than the id, so the two markers
    // change length by different amounts and neither can be assumed.
    expect(replaced.map(([, to]) => to)).toEqual([
      '[@claude](avibe-mention:agent:claude)',
      '[#Bridge](avibe-mention:session:ses6jr7c5h2q6)',
      '[@claude](avibe-mention:agent:claude)',
    ]);
  });

  it('reports nothing when there is nothing to rewrite', () => {
    const input = 'No markers here, just an email@example.com and a # sign.';
    const { text, edits } = linkifyMentions(input, REFERENCES);

    expect(text).toBe(input);
    expect(edits).toEqual([]);
  });

  it('skips markers inside code, and still measures the ones outside it', () => {
    // The offsets after a skipped code span are the ones a naive per-segment
    // counter gets wrong, so a marker on each side of one is the case worth
    // measuring.
    const input = 'Type `@<claude>` to reach @<claude>, or ```\n#<ses6jr7c5h2q6>\n``` first.';
    const { text, edits } = linkifyMentions(input, REFERENCES);

    const replaced = verifyEdits(input, text, edits);
    expect(replaced.map(([from]) => from)).toEqual(['@<claude>']);
    expect(text).toContain('`@<claude>`');
    expect(text).toContain('#<ses6jr7c5h2q6>\n```');
  });

  it('measures in UTF-16 code units, past an astral character', () => {
    const input = '🙂 ask @<claude>.';
    const { text, edits } = linkifyMentions(input, REFERENCES);

    verifyEdits(input, text, edits);
    expect(edits[0].start).toBe(7);
    expect(input.slice(edits[0].start, edits[0].end)).toBe('@<claude>');
  });

  it('falls back to the session id when no title was captured', () => {
    const input = 'See #<ses6jr7c5h2q6>.';
    const { text, edits } = linkifyMentions(input);

    const replaced = verifyEdits(input, text, edits);
    expect(replaced[0][1]).toBe('[#ses6jr7c5h2q6](avibe-mention:session:ses6jr7c5h2q6)');
  });
});
