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
const mention = () => section('const SHADOW_MENTIONS = [', '\n];');

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

  // And the same fact for the other language, which is why that `i` cannot be
  // one flag over the whole matcher. CSS folds case; JavaScript does not, so
  // `BOXSHADOW` and `boxshadow` are keys that address no CSS property and draw
  // nothing at all. Measuring them case-blind reported two names the channel
  // could not claim -- a false positive on code with no CSS in it -- and hid
  // that the real CSSOM spelling `webkitBoxShadow` was missing from the property
  // list, by matching its capitalised alias case-blind instead.
  //
  // Asserted as the shared EXPRESSION rather than as two flags that agree
  // today: the JS half of the measurement is the JS channel's pattern, written
  // the same way in both places, so neither can gain an `i` without the other.
  // That is the same discipline the test below applies to the key itself, one
  // level down -- a copy that has to be edited twice is a copy that will be
  // edited once.
  it('measures JavaScript keys case-sensitively, because JavaScript is', () => {
    const jsPattern = "new RegExp(SHADOW_KEY, 'g')";

    expect(mention()).toContain(jsPattern);
    expect(channels()).toContain(jsPattern);
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
  //
  // What that expression actually reads is asserted in `styleWrite.test.mjs`,
  // where `propertyExpression` can be called; this is the half that has to stay
  // here, because "which reader this channel uses" is a fact about the channel.
  it('reads a style property\'s own expression rather than the rest of the line', () => {
    // Selected by how its pattern STARTS, not by mentioning the key: the
    // preceding channel's trailing comment quotes that spelling in prose, and a
    // substring search happily returns the wrong channel.
    const inline = eachChannel().find((channel) =>
      channel.trimStart().startsWith('new RegExp(SHADOW_KEY')
    );

    expect(inline, 'the inline-style channel is no longer spelled this way').toBeDefined();
    expect(inline).not.toContain('[^;\\n]');
    expect(inline).toContain('styleExpression');
  });

  // The third thing the two readers have to agree on, after the property name
  // and the flags: which JavaScript is a style write at all. `const cardShadow =
  // 'compact'` was read as one by both, and narrowing only the channel would
  // have left the mention counting a span nothing claims -- the "unscanned
  // channel" failure this file was written about, arriving from the other side.
  //
  // Sharing a constant was the first answer and it was not enough. The two still
  // COMPOSED it differently -- one added a quote here, the other a bracket there
  // -- and three rounds of keeping those compositions in step by hand produced
  // three rounds of them drifting. The mention matcher now embeds the channel's
  // whole key, so there is no composition left to keep in step: they are one
  // regex, and a spelling the channel gains is a spelling the measurement gains
  // in the same edit.
  // The fifth quantity, after the property name, the flags, the key and the
  // case rules: WHERE a match begins. A channel may legitimately claim less
  // text than the mention points at -- `drop-shadow-[…]` is read for what is
  // inside the brackets, so the claim starts at `shadow-[` while the mention
  // starts at `drop-` -- and comparing start offsets called that unscanned,
  // failing a correct, fully tokenized utility. Two spans that cover the same
  // text is what "some channel read this" actually means.
  it('counts a mention as claimed when a channel read the same text', () => {
    const loop = section('for (const pattern of SHADOW_MENTIONS) {', '\n    }');

    expect(loop).toContain('from < end && to > start');
    expect(loop).not.toContain('mention.index >= start');
  });

  it('measures a style write with the very key the channel reads it by', () => {
    expect(mention()).toContain('SHADOW_KEY');

    const inline = eachChannel().find((channel) =>
      channel.trimStart().startsWith('new RegExp(SHADOW_KEY')
    );
    expect(inline, 'the inline-style channel is no longer spelled this way').toBeDefined();
  });
});
