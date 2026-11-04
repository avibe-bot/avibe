import { describe, expect, it } from 'vitest';

import { EXCERPT_MAX_CHARS, messageExcerpt } from './messageExcerpt';

// The excerpt a Bot's strip card and switch sheet show for its latest message.
describe('messageExcerpt', () => {
  it.each([
    ['headings, lists, quotes and emphasis', '# Plan\n\n1. **Ship** it\n2. _Then_ rest\n\n> soon', 'Plan Ship it Then rest soon'],
    ['code keeps its text on the one line', 'Run:\n```sh\nnpm ci\nnpm test\n```\nand `vibe status`', 'Run: npm ci npm test and vibe status'],
    ['links and images keep their labels', 'See [the PR](https://x.test/1) and ![chart](c.png)', 'See the PR and chart'],
    ['runs of whitespace become one space', '  one\n\n\ttwo   three  ', 'one two three'],
    ['nothing said', null, ''],
  ])('reads %s as plain text', (_case, text, excerpt) => {
    expect(messageExcerpt(text)).toBe(excerpt);
  });

  it(`cuts a long message to ${EXCERPT_MAX_CHARS} characters, ending in an ellipsis`, () => {
    const long = '进度更新：'.repeat(60);
    const excerpt = messageExcerpt(long);
    expect(Array.from(excerpt)).toHaveLength(EXCERPT_MAX_CHARS);
    expect(excerpt.endsWith('…')).toBe(true);
    expect(long.startsWith(excerpt.slice(0, -1))).toBe(true);
    // A character outside the BMP is never split in half.
    expect(messageExcerpt('🙂'.repeat(EXCERPT_MAX_CHARS + 5))).toBe(`${'🙂'.repeat(EXCERPT_MAX_CHARS - 1)}…`);
  });
});
