import { describe, expect, it } from 'vitest';

import {
  glowOffencesInValue,
  readsIntoDropShadow,
  spreadOffencesInDropShadow,
} from './shadowLayer.mjs';

// The rule the whole gate is about: a glow may not be written by hand, and a
// layer that cannot be shown not to be one fails asking to be made legible.
//
// Ten documented rounds of holes in this classifier were found by a reviewer
// rather than by a test, because it lived inside a script that reads the tree
// the moment it is imported -- there was nothing to call. Both directions cost
// real rounds. A hole ships a hand-drawn glow that light cannot re-anchor; a
// false positive fails a pull request that spelled the required token exactly
// right, and this round produced two of those at once.
//
// So the cases below are the property in both directions, stated as the rules
// they are examples of rather than as the spellings that happened to be found.

// The stylesheet declarations the classifier resolves names against. Built per
// test rather than shared, so one test's token cannot leak into another's.
const declaring = ({ values = {}, managed = {}, colours = [] } = {}) => ({
  values: new Map(Object.entries(values).map(([name, list]) => [name, new Set([list].flat())])),
  managed: new Map(Object.entries(managed).map(([name, list]) => [name, new Set([list].flat())])),
  colours: new Set(colours),
});

const MANAGED = declaring({
  values: { '--shadow-glow-md-mint': '0 0 16px -4px rgba(91, 255, 160, 0.44)' },
  managed: { '--shadow-glow-md-mint': '0 0 16px -4px rgba(91, 255, 160, 0.44)' },
});

const offences = (value, tokens = MANAGED) => glowOffencesInValue(value, tokens);
const accepts = (value, tokens = MANAGED) => expect(offences(value, tokens)).toEqual([]);
const rejects = (value, tokens = MANAGED) => expect(offences(value, tokens).length).toBeGreaterThan(0);

describe('what the classifier accepts', () => {
  // The shape this gate exists to require. Everything else in this block is a
  // spelling of it, or a shadow that provably is not a glow.
  it('accepts a managed glow token', () => {
    accepts('var(--shadow-glow-md-mint)');
  });

  it.each([
    ['none', 'none'],
    ['inherit', 'inherit'],
  ])('accepts the CSS-wide keyword %s', (_label, value) => accepts(value));

  it('accepts an offset shadow, which is directional light rather than a glow', () => {
    accepts('0 2px 8px rgba(0, 0, 0, 0.4)');
  });

  // Whether a layer is centred is decided by its OFFSETS and nothing else, so a
  // part that cannot move them cannot make the answer uncertain. The
  // deny-by-default rule below rejected a math function anywhere in the layer
  // before the offsets were read at all, so a directional shadow whose literal
  // `4px` already settled the question failed for computing a blur, a spread or
  // a colour channel -- three spellings of one mistake, which is why they are
  // stated as three rather than as the reported one.
  it.each([
    ['blur', '0 4px calc(8px) red'],
    ['spread', '0 4px 8px calc(2px) red'],
    ['colour channel', '0 4px 8px rgb(calc(255 / 2) 0 0)'],
  ])('accepts a shadow whose offsets prove it directional while its %s computes', (_label, value) =>
    accepts(value)
  );

  it('accepts a ring, which has no blur to colour-manage', () => {
    accepts('0 0 0 2px rgba(0, 0, 0, 0.4)');
  });

  // `!important` belongs to the DECLARATION, not to any layer in it. Round ten
  // taught this to the CSSOM branch and left the CSS one behind, so the same
  // token failed depending on which language wrote it.
  it('accepts a managed token carrying !important', () => {
    accepts('var(--shadow-glow-md-mint) !important');
  });

  // This round's first false positive, and the one that is hardest to argue
  // with: the guard rejected a word order of the very thing it is asking for.
  // `inset` is a keyword of the shadow grammar with two freedoms the parser has
  // to grant -- the spec allows it before the lengths or after the colour, and
  // CSS keywords are ASCII case-insensitive.
  //
  // Stated as all four spellings rather than the one that was reported, because
  // the reported one was only the position half; filtering position without
  // case would have closed the finding and left the other half open.
  it.each([
    ['leading', 'inset var(--shadow-glow-md-mint)'],
    ['trailing', 'var(--shadow-glow-md-mint) inset'],
    ['leading, capitalised', 'INSET var(--shadow-glow-md-mint)'],
    ['trailing, mixed case', 'var(--shadow-glow-md-mint) Inset'],
  ])('accepts a managed token with %s inset', (_label, value) => accepts(value));

  // The second false positive. The blur slot is proved innocent by showing the
  // part in it is a COLOUR, and that recogniser is an enumeration of CSS
  // Color's functional notations -- deliberately, because it sits on the accept
  // side, where a missing name costs a spurious failure rather than a glow that
  // ships. `light-dark()` arriving is that enumeration behaving as designed.
  it.each([
    ['light-dark', '0 0 light-dark(red, blue)'],
    ['oklch', '0 0 oklch(0.7 0.2 150)'],
    ['color-mix', '0 0 color-mix(in oklab, red, blue)'],
    ['contrast-color', '0 0 contrast-color(red)'],
    ['device-cmyk', '0 0 device-cmyk(0 0.5 1 0)'],
  ])('accepts a blur-free layer coloured with %s()', (_label, value) => accepts(value));

  it('accepts a name registered as a colour in the blur slot', () => {
    accepts('0 0 var(--tint)', declaring({ colours: ['--tint'] }));
  });

  // The fallback is part of the value, not a separate spelling to exclude.
  // Accepting the registration only when it stood alone read `var(--tint, blue)`
  // as unprovable and failed a shadow where NEITHER possible value can occupy
  // the blur slot -- the registered name cannot hold a length, and `blue` is a
  // colour. Both halves have to be answered, and each hop asks the same
  // question, so a fallback that is itself a registered name is answered too.
  it('accepts a registered colour whose fallback is also a colour', () => {
    accepts('0 0 var(--tint, blue)', declaring({ colours: ['--tint'] }));
  });

  it('accepts a registered colour whose fallback is a colour function', () => {
    accepts('0 0 var(--tint, color-mix(in oklab, red, blue))', declaring({ colours: ['--tint'] }));
  });

  it('accepts a registered colour falling back to another registered colour', () => {
    accepts('0 0 var(--tint, var(--other))', declaring({ colours: ['--tint', '--other'] }));
  });
});

describe('what the classifier rejects', () => {
  it('rejects a hand-written glow', () => {
    rejects('0 0 16px -4px rgba(91, 255, 160, 0.44)');
  });

  // The colour is allowed to lead in CSS, and exempting a leading `var()`
  // reopened the hole from the other end: the geometry went unread.
  it('rejects a hand-written glow whose colour leads', () => {
    rejects('var(--mint) 0 0 93px');
  });

  // Deny by default, one level down: an offset this scan cannot evaluate has no
  // innocent answer to give, and `calc(0px)` is not the literal `0` a zero-test
  // looks for. The exemption above is for maths that provably cannot reach the
  // offsets; maths IN one of them, or beside two zeroes, is the case the
  // exemption must not widen to cover.
  it('rejects offsets it cannot evaluate', () => {
    rejects('calc(0px) calc(0px) 93px red');
  });

  it('rejects a centred layer whose blur is computed', () => {
    rejects('0 0 calc(93px) red');
  });

  // A name could hold any radius, so it is unprovable rather than innocent --
  // unless `@property` has registered it as a colour, which the accepting test
  // above covers.
  it('rejects a name in the blur slot', () => {
    rejects('0 0 var(--blur)');
  });

  // The other half of reading the fallback: it is a second value that renders
  // whenever the registration does not, so it has to be a colour too. A
  // registration cannot vouch for a value it does not constrain.
  it('rejects a registered colour whose fallback is a length', () => {
    rejects('0 0 var(--tint, 93px)', declaring({ colours: ['--tint'] }));
  });

  it('rejects a registered colour falling back to an unregistered name', () => {
    rejects('0 0 var(--tint, var(--blur))', declaring({ colours: ['--tint'] }));
  });

  // Managed is a PLACE, not a prefix. A name that only looks managed falls
  // through to the ordinary classification.
  it('rejects a glow hidden behind a name that merely looks managed', () => {
    rejects('var(--shadow-glow-rogue)', declaring({
      values: { '--shadow-glow-rogue': '0 0 93px red' },
    }));
  });

  it('rejects a glow one alias deeper', () => {
    rejects('var(--card)', declaring({
      values: { '--card': 'var(--rogue)', '--rogue': '0 0 93px red' },
    }));
  });

  it('rejects a name declared in no scanned stylesheet', () => {
    rejects('var(--nowhere)');
  });

  // A fallback renders whenever the token it guards does not, so it is a second
  // value and is classified even beside a managed name.
  it('rejects a glow in a fallback beside a managed token', () => {
    rejects('var(--shadow-glow-md-mint, 0 0 93px red)');
  });

  // Every layer is classified, so a glow cannot ride along beside an innocent
  // one.
  it('rejects a glow in the second layer of a list', () => {
    rejects('0 2px 8px rgba(0, 0, 0, 0.4), 0 0 93px red');
  });
});

// `drop-shadow()` has no spread slot. A filter function given a length too many
// is invalid, so the browser drops the layer whole and the glow does not render
// AT ALL -- a silent nothing rather than a wrong something, which is why the
// glow classifier could never see it: the value it reads is a correct, managed
// token. The constraint belongs to the CALL SITE, and these are the two facts
// that follow from that.
describe('what drop-shadow() refuses to carry', () => {
  const SIZED = declaring({
    values: {
      '--shadow-glow-md-mint': '0 0 24px -6px rgba(91, 255, 160, 0.44)',
      '--shadow-glow-wire-mint': '0 0 4px rgba(91, 255, 160, 0.44)',
      '--indirect': 'var(--shadow-glow-md-mint)',
    },
    managed: {
      '--shadow-glow-md-mint': '0 0 24px -6px rgba(91, 255, 160, 0.44)',
      '--shadow-glow-wire-mint': '0 0 4px rgba(91, 255, 160, 0.44)',
    },
  });
  const spreads = (value) => spreadOffencesInDropShadow(value, SIZED);

  // The whole point of the separate walk: the glow classifier stops AT a managed
  // token because its geometry is read against design.pen, and that is exactly
  // the wrong stop for a question the token cannot answer about itself.
  it('opens a managed token the glow walk is finished with', () => {
    expect(spreads('var(--shadow-glow-md-mint)').length).toBe(1);
    expect(glowOffencesInValue('var(--shadow-glow-md-mint)', SIZED)).toEqual([]);
  });

  it('follows a spread one alias deeper', () => {
    expect(spreads('var(--indirect)').length).toBe(1);
  });

  // Stated as the property, so a hand-written layer that names no role at all
  // fails for the same reason a sized token does.
  it('rejects a hand-written layer carrying a spread', () => {
    expect(spreads('2px 2px 4px 2px red').length).toBe(1);
  });

  it('accepts a spreadless role', () => {
    expect(spreads('var(--shadow-glow-wire-mint)')).toEqual([]);
  });

  it('accepts a spreadless hand-written layer', () => {
    expect(spreads('0 0 4px red')).toEqual([]);
  });

  // A keyword and a priority are both parts that are not lengths, and either one
  // left in place turns a one-part layer into a multi-part one -- which is a
  // count of lengths that never reaches the token, so the spread inside it goes
  // unread. Both are dropped for the same reason the glow walk drops them, and
  // this is the direction that matters: a MISS, not a spurious failure.
  it('reads the token behind a keyword', () => {
    expect(spreads('inset var(--shadow-glow-md-mint)').length).toBe(1);
  });

  it('reads the token behind !important', () => {
    expect(spreads('var(--shadow-glow-md-mint) !important').length).toBe(1);
  });

  // Every layer is asked, so a spread cannot ride along beside a spreadless one.
  it('finds a spread in the second layer of a list', () => {
    expect(spreads('0 0 4px red, 0 0 4px 2px blue').length).toBe(1);
  });
});

// Which matches that rule applies to. The four spellings all reach the same
// function, and only two of them carry the `drop-` prefix inside the match --
// which is why this is asked of the source text rather than of the channel.
describe('which spellings read into drop-shadow()', () => {
  const reads = (source, needle) => readsIntoDropShadow(source, source.indexOf(needle), needle);

  it.each([
    ['the filter function', 'filter: drop-shadow(0 0 4px red)', 'drop-shadow('],
    ['the custom-property shorthand', '<i class="drop-shadow-(--x)" />', 'drop-shadow-(--x)'],
    ['the Tailwind prefix, whose match starts after it', 'class="drop-shadow-[var(--x)]"', 'shadow-[var(--x)]'],
    ['a capitalised filter function', 'filter: DROP-SHADOW(0 0 4px red)', 'DROP-SHADOW('],
  ])('reads %s', (_label, source, needle) => expect(reads(source, needle)).toBe(true));

  // And the half that has to stay false: `box-shadow` DOES take a spread, so
  // answering yes here would restrict every sized token on the page.
  it.each([
    ['a box-shadow utility', 'class="shadow-[var(--x)]"', 'shadow-[var(--x)]'],
    ['a box-shadow declaration', 'box-shadow: 0 0 4px 2px red', 'box-shadow:'],
    ['a shorthand with no prefix', '<i class="shadow-(--x)" />', 'shadow-(--x)'],
  ])('does not read %s', (_label, source, needle) => expect(reads(source, needle)).toBe(false));
});
