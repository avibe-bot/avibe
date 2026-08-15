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

// One entry per channel: the text from its `pattern:` to the next channel's.
// That span carries the pattern together with the `valuesOf` and
// `provablyNotAShadow` that interpret it, which is the unit these assertions are
// about -- a pattern is only half of what a channel reads.
const eachChannel = () => {
  const entries = channels().split('pattern:').slice(1);
  expect(entries.length, 'SHADOW_CHANNELS has no entries').toBeGreaterThan(0);
  return entries;
};

const propertyFlags = () => {
  const match = SCAN.match(/^const PROPERTY_FLAGS = '([a-z]*)';/m);
  expect(match, 'PROPERTY_FLAGS is no longer spelled that way').not.toBeNull();
  return match[1];
};

describe('the shadow channels and the completeness matcher', () => {
  it('define what a shadow property is exactly once', () => {
    expect([...SCAN.matchAll(/^const SHADOW_PROPERTY = /gm)]).toHaveLength(1);
  });

  // Directly, or through CSS_SHADOW_PROPERTY -- which is that same constant with
  // a hyphen required, asserted below to be spelled exactly that way, so the
  // tail rule still has one home either way.
  it('both derive their property name from it', () => {
    expect(channels()).toMatch(/\$\{(?:CSS_)?SHADOW_PROPERTY\}/);
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

  // The flags are half of what a matcher means, and they diverged after the
  // pattern stopped being able to: the CSS channels ran case-sensitively while
  // the mention matcher ran `gi`, so `BOX-SHADOW:` -- valid CSS, property names
  // being ASCII case-insensitive -- was measured as a mention no channel read.
  it('reads CSS property names case-insensitively, like the mention matcher does', () => {
    expect(propertyFlags()).toContain('i');
    expect(mention()).toContain("'gi'");

    const css = eachChannel().filter((channel) => channel.includes('${CSS_SHADOW_PROPERTY}'));

    expect(css.length).toBeGreaterThanOrEqual(2);
    for (const channel of css) expect(channel).toContain('PROPERTY_FLAGS');
  });

  // What makes that `i` safe. One set of channels reads three languages, so a
  // case-insensitive CSS property name also matches the camelCase JS key -- and
  // reading a JS expression with CSS's terminators stops at the first quote,
  // which fails the tree this guard is guarding. Every CSS shadow property
  // carries a hyphen and no JS style key does, so requiring it separates them.
  it('keeps the CSS channels off camelCase style keys', () => {
    expect(SCAN).toContain('const CSS_SHADOW_PROPERTY = `(?=[A-Za-z-]*-shadow)${SHADOW_PROPERTY}`');

    for (const channel of eachChannel()) {
      if (channel.includes('PROPERTY_FLAGS')) expect(channel).toContain('${CSS_SHADOW_PROPERTY}');
    }
  });

  // A style property's value is its own expression. Capturing `[^;\n]*` -- the
  // rest of the line -- swept the NEXT property up as a second shadow layer, so
  // `style={{ boxShadow: 'none', color: 'red' }}` failed on `red`.
  it('reads a style property\'s own expression rather than the rest of the line', () => {
    // Selected by how its pattern STARTS, not by mentioning `[Ss]hadow`: the
    // preceding channel's trailing comment quotes that spelling in prose, and a
    // substring search happily returns the wrong channel.
    const inline = eachChannel().find((channel) =>
      channel.trimStart().startsWith('/(?<![\\w-])[A-Za-z]*[Ss]hadow')
    );

    expect(inline, 'the inline-style channel is no longer spelled this way').toBeDefined();
    expect(inline).not.toContain('[^;\\n]');
    expect(inline).toContain('styleExpression');
  });
});
