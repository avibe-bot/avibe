import fs from 'node:fs';

import { describe, expect, it } from 'vitest';

// `validate:theme` reads a source file twice: the CHANNELS extract the values a
// shadow can be introduced with, and SHADOW_MENTION measures whether every place
// one could have been introduced was reached by some channel. The second exists
// to make the first's gaps loud instead of silent.
//
// That only works while the two agree on what a shadow property is. They did
// not: the CSS declaration channel read `box-shadow` and only it, while the
// mention matcher read any `*shadow`, so `text-shadow: var(--shadow-glow-md-mint)`
// -- correct, fully tokenized CSS -- was measured as a mention, matched by no
// channel, and reported as an unscanned channel. The guard failed a file that had
// done exactly what it asks for.
//
// The comment above SHADOW_MENTION had already predicted this ("the two must
// narrow together"). A comment is not a mechanism. They now derive from one
// SHADOW_PROPERTY constant, and this asserts that they still do -- so the next
// property that ends in the word is covered by both sides at once, rather than
// by whichever one someone remembered to widen.
const SCAN = fs.readFileSync(new URL('validate-theme.mjs', import.meta.url), 'utf8');

// Each reader sliced out by its own name, so an assertion about one cannot be
// satisfied by the other -- counting `${SHADOW_PROPERTY}` across the whole file
// is exactly the check that stays green while one of the two drifts off.
//
// The closing delimiter is searched FROM the opening one. `indexOf('\n];')` from
// zero finds an earlier array, and the empty slice that leaves matches nothing
// and passes for the wrong reason; the non-vacuity assertions below are what
// makes a slice that failed to find its target fail the test instead.
const section = (opening, closing) => {
  const start = SCAN.indexOf(opening);
  expect(start, `\`${opening}\` is no longer spelled that way`).toBeGreaterThan(-1);

  const end = SCAN.indexOf(closing, start);
  expect(end, `\`${opening}\` is not closed by \`${closing.trim()}\``).toBeGreaterThan(start);

  return SCAN.slice(start, end);
};

const channels = () => section('const SHADOW_CHANNELS = [', '\n];');
const mention = () => section('const SHADOW_MENTION = ', '\n);');

describe('the shadow channels and the completeness matcher', () => {
  it('define what a shadow property is exactly once', () => {
    expect([...SCAN.matchAll(/^const SHADOW_PROPERTY = /gm)]).toHaveLength(1);
  });

  it('both derive their property name from it', () => {
    expect(channels()).toContain('${SHADOW_PROPERTY}');
    expect(mention()).toContain('${SHADOW_PROPERTY}');
  });

  // The way back to two spellings is for a channel to name a property inline
  // again. A declaration is a name followed by a colon, which is the shape that
  // diverged; naming one inside a comment or an offence message is prose and
  // stays free.
  it('leaves no channel matching a hardcoded property name', () => {
    const hardcoded = [...channels().matchAll(/pattern:.*?[A-Za-z-]+shadow\\s\*:/g)].map((match) =>
      match[0].trim()
    );

    expect(hardcoded).toEqual([]);
  });
});
