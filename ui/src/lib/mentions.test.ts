import { describe, expect, it } from 'vitest';

import { linkifyMentions, parseMentionHref, referenceToMarker, type MentionReference } from './mentions';

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

  // Typed markers (docs/plans/bots.md D3): the marker carries the kind and the
  // frozen handle; the chip label is the Bot's display name from the sidecar,
  // so a rename never has to rewrite a stored message.
  it('reads typed Bot and Agent markers, and an untyped legacy one as an Agent', () => {
    const input = 'Ask @<bot:cso>, @<agent:reviewer>, and @<claude>.';
    const { text, edits } = linkifyMentions(input, [
      ...REFERENCES,
      { kind: 'bot', handle: 'cso', display_name: '首席安全官' },
    ]);

    const replaced = verifyEdits(input, text, edits);
    expect(replaced.map(([, to]) => to)).toEqual([
      '[@首席安全官](avibe-mention:bot:cso)',
      '[@reviewer](avibe-mention:agent:reviewer)',
      '[@claude](avibe-mention:agent:claude)',
    ]);
    expect(parseMentionHref('avibe-mention:bot:cso')).toEqual({ kind: '@', target: 'bot', value: 'cso' });
    expect(parseMentionHref('avibe-mention:agent:reviewer')).toEqual({ kind: '@', target: 'agent', value: 'reviewer' });
  });

  it('labels a Bot by its handle when the message carried no display name', () => {
    const { text } = linkifyMentions('Ask @<bot:cso>.', [{ kind: 'bot', handle: 'cso' }]);

    expect(text).toBe('Ask [@cso](avibe-mention:bot:cso).');
  });

  it('in agentReply mode, links resolved bracketed and bare sessions, keeps unresolved plain text, and tracks exact edits', () => {
    const input = '好，已经起了一个后台 Agent（#sesw34zgzgcvp），另看 #<ses6jr7c5h2q6>、@<bot:product>，忽略 #<sesmissing99>、#sesmissing88 和 `#sesw34zgzgcvp`。';
    const { text, edits } = linkifyMentions(
      input,
      [
        { kind: 'session', session_id: 'sesw34zgzgcvp', title: '登录跳转修复', openable: true },
        { kind: 'session', session_id: 'ses6jr7c5h2q6', title: 'Bridge', openable: true },
        { kind: 'bot', handle: 'product', display_name: '产品伙伴' },
      ],
      { agentReply: true },
    );

    const replaced = verifyEdits(input, text, edits);
    expect(replaced).toEqual([
      ['#sesw34zgzgcvp', '[#登录跳转修复](avibe-mention:session:sesw34zgzgcvp)'],
      ['#<ses6jr7c5h2q6>', '[#Bridge](avibe-mention:session:ses6jr7c5h2q6)'],
      ['@<bot:product>', '[@产品伙伴](avibe-mention:bot:product)'],
      ['#<sesmissing99>', '#\\<sesmissing99>'],
    ]);
    // Unresolved bare id and inline code remain completely untouched.
    expect(text).toContain('#sesmissing88');
    expect(text).toContain('`#sesw34zgzgcvp`');
  });

  it('in agentReply mode, skips URL fragments, markdown links, real indented code blocks, and line-start unclosed fences', () => {
    const input =
      'See https://example.com/#sesw34zgzgcvp, [see #sesw34zgzgcvp](https://example.com), '
      + '[notes for #<sesw34zgzgcvp>](https://example.com), '
      + '[doc](#sesw34zgzgcvp), and #sesw34zgzgcvp.\n\n'
      + '    #<sesw34zgzgcvp>\n    #<sesmissing99>\n\t#sesw34zgzgcvp\n\n'
      + '```ts\n#sesw34zgzgcvp\n#<sesmissing99>';
    const { text, edits } = linkifyMentions(
      input,
      [{ kind: 'session', session_id: 'sesw34zgzgcvp', title: 'Auth Fix', openable: true }],
      { agentReply: true },
    );

    const replaced = verifyEdits(input, text, edits);
    expect(replaced).toEqual([
      ['#<sesw34zgzgcvp>', '#\\<sesw34zgzgcvp>'],
      ['#sesw34zgzgcvp', '[#Auth Fix](avibe-mention:session:sesw34zgzgcvp)'],
    ]);
    expect(text).toContain('https://example.com/#sesw34zgzgcvp');
    expect(text).toContain('[see #sesw34zgzgcvp](https://example.com)');
    expect(text).toContain('[notes for #\\<sesw34zgzgcvp>](https://example.com)');
    expect(text).toContain('[doc](#sesw34zgzgcvp)');
    expect(text).toContain('    #<sesw34zgzgcvp>\n    #<sesmissing99>\n\t#sesw34zgzgcvp');
    expect(text).toContain('```ts\n#sesw34zgzgcvp\n#<sesmissing99>');
  });

  it('in agentReply mode, matches closing fence length so 4-backtick fences protect inner 3-backtick examples', () => {
    const input =
      '````markdown\n```\n#<sesw34zgzgcvp>\n```\n#<sesw34zgzgcvp>\n#<sesmissing99>\n````\n\nAfter #<sesw34zgzgcvp>.';
    const { text, edits } = linkifyMentions(
      input,
      [{ kind: 'session', session_id: 'sesw34zgzgcvp', title: 'Auth Fix', openable: true }],
      { agentReply: true },
    );

    const replaced = verifyEdits(input, text, edits);
    expect(replaced).toEqual([
      ['#<sesw34zgzgcvp>', '[#Auth Fix](avibe-mention:session:sesw34zgzgcvp)'],
    ]);
    expect(text).toContain('````markdown\n```\n#<sesw34zgzgcvp>\n```\n#<sesw34zgzgcvp>\n#<sesmissing99>\n````');
  });

  it('in agentReply mode, skips indented fenced blocks in lists, closes empty and CRLF fences, and keeps state across mid-line fences', () => {
    const input =
      '10. Step\n\n    ```py\n    x=1\n\n    print("#<sesw34zgzgcvp>")\n    ```\n\n'
      + '1. Step\n   - sub\n\n     ```py\n     x=1\n\n     print("#<sesw34zgzgcvp>")\n     ```\n\n'
      + '```\n```\nSee #<sesw34zgzgcvp>\n\n'
      + '```py\r\nx=1\r\n```\r\nAfter CRLF #<sesw34zgzgcvp>\n\n'
      + 'Intro\n\n    a ```x``` b\n    more #<sesw34zgzgcvp>\n\n'
      + '- run ```cmd``` first\n\n    continuation #<sesw34zgzgcvp>';
    const { text, edits } = linkifyMentions(
      input,
      [{ kind: 'session', session_id: 'sesw34zgzgcvp', title: 'Auth Fix', openable: true }],
      { agentReply: true },
    );

    const replaced = verifyEdits(input, text, edits);
    expect(replaced).toEqual([
      ['#<sesw34zgzgcvp>', '[#Auth Fix](avibe-mention:session:sesw34zgzgcvp)'],
      ['#<sesw34zgzgcvp>', '[#Auth Fix](avibe-mention:session:sesw34zgzgcvp)'],
      ['#<sesw34zgzgcvp>', '[#Auth Fix](avibe-mention:session:sesw34zgzgcvp)'],
    ]);
    expect(text).toContain('print("#<sesw34zgzgcvp>")');
    expect(text).toContain('    more #<sesw34zgzgcvp>');
  });

  it('in agentReply mode, preserves multiline inline code spans and backtick spans with inner backticks', () => {
    const input =
      'Run `echo\n#<sesw34zgzgcvp>\n#sesw34zgzgcvp` and ``quote ` #<sesmissing99>\n#<sesw34zgzgcvp>`` then #<sesw34zgzgcvp>.';
    const { text, edits } = linkifyMentions(
      input,
      [{ kind: 'session', session_id: 'sesw34zgzgcvp', title: 'Auth Fix', openable: true }],
      { agentReply: true },
    );

    const replaced = verifyEdits(input, text, edits);
    expect(replaced).toEqual([
      ['#<sesw34zgzgcvp>', '[#Auth Fix](avibe-mention:session:sesw34zgzgcvp)'],
    ]);
    expect(text).toContain('`echo\n#<sesw34zgzgcvp>\n#sesw34zgzgcvp`');
    expect(text).toContain('``quote ` #<sesmissing99>\n#<sesw34zgzgcvp>``');
  });

  it('in agentReply mode, still resolves nested 4-space bullets, list/paragraph continuations, and prose after mid-sentence triple backticks', () => {
    const input =
      '- parent\n'
      + '    - child sees #<sesw34zgzgcvp> and #ses6jr7c5h2q6\n'
      + '1. step\n'
      + '    ordered continuation #<sesw34zgzgcvp>\n'
      + '\n'
      + '    loose list continuation #ses6jr7c5h2q6\n'
      + '\n'
      + 'Para line\n'
      + '    lazy continuation #<sesw34zgzgcvp>\n'
      + '\n'
      + 'Use ``` to open a fence. Then #<ses6jr7c5h2q6>.';
    const { text, edits } = linkifyMentions(
      input,
      [
        { kind: 'session', session_id: 'sesw34zgzgcvp', title: 'Auth Fix', openable: true },
        { kind: 'session', session_id: 'ses6jr7c5h2q6', title: 'Bridge', openable: true },
      ],
      { agentReply: true },
    );

    const replaced = verifyEdits(input, text, edits);
    expect(replaced).toEqual([
      ['#<sesw34zgzgcvp>', '[#Auth Fix](avibe-mention:session:sesw34zgzgcvp)'],
      ['#ses6jr7c5h2q6', '[#Bridge](avibe-mention:session:ses6jr7c5h2q6)'],
      ['#<sesw34zgzgcvp>', '[#Auth Fix](avibe-mention:session:sesw34zgzgcvp)'],
      ['#ses6jr7c5h2q6', '[#Bridge](avibe-mention:session:ses6jr7c5h2q6)'],
      ['#<sesw34zgzgcvp>', '[#Auth Fix](avibe-mention:session:sesw34zgzgcvp)'],
      ['#<ses6jr7c5h2q6>', '[#Bridge](avibe-mention:session:ses6jr7c5h2q6)'],
    ]);
  });

  it('escapes angle brackets and backticks in resolved titles so autolinks cannot nest', () => {
    const input = 'See #<sesw34zgzgcvp>.';
    const { text, edits } = linkifyMentions(
      input,
      [{ kind: 'session', session_id: 'sesw34zgzgcvp', title: 'Check <https://evil.test> `code`', openable: true }],
      { agentReply: true },
    );

    const replaced = verifyEdits(input, text, edits);
    expect(replaced).toEqual([
      ['#<sesw34zgzgcvp>', '[#Check \\<https://evil.test\\> \\`code\\`](avibe-mention:session:sesw34zgzgcvp)'],
    ]);
  });
});

describe('referenceToMarker', () => {
  it('writes the kind into every @ marker', () => {
    expect(referenceToMarker({ kind: 'bot', handle: 'cso', display_name: '首席安全官' })).toBe('@<bot:cso>');
    expect(referenceToMarker({ kind: 'agent', name: 'reviewer' })).toBe('@<agent:reviewer>');
    expect(referenceToMarker({ kind: 'session', session_id: 'ses6jr7c5h2q6' })).toBe('#<ses6jr7c5h2q6>');
  });
});
