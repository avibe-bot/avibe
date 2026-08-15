import { describe, expect, it } from 'vitest';

import { glowOffencesInValue } from './shadowLayer.mjs';

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
  // looks for.
  it('rejects offsets it cannot evaluate', () => {
    rejects('calc(0px) calc(0px) 93px red');
  });

  // A name could hold any radius, so it is unprovable rather than innocent --
  // unless `@property` has registered it as a colour, which the accepting test
  // above covers.
  it('rejects a name in the blur slot', () => {
    rejects('0 0 var(--blur)');
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
