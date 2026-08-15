import fs from 'node:fs';

import { describe, expect, it } from 'vitest';

import { isLength, isZeroLength } from './cssLength.mjs';

// The unit list this replaces held six of the thirty-odd units CSS accepts, so
// `box-shadow: 1cm 1cm 4px red` -- a directional shadow with no glow in it --
// failed `validate:theme` for a pull request that was correct. Listing thirty
// instead of six would only move the boundary: the units that break it next are
// the ones not invented yet.
//
// So these cases are examples of a rule, not the rule itself. The rule is
// asserted directly below them: whatever else changes, the unit may not become
// an enumeration again.

// One per family, including the ones the old list omitted and the ones added to
// CSS most recently.
const LENGTHS = [
  '0',
  '4px',
  '1cm',
  '2pt',
  '3pc',
  '0.5in',
  '10mm',
  '4Q',
  '1.5rem',
  '2em',
  '3ex',
  '4ch',
  '1cap',
  '2ic',
  '1lh',
  '2rlh',
  '50vw',
  '50vh',
  '10vmin',
  '10vmax',
  '5svh',
  '5lvh',
  '5dvh',
  '5vi',
  '5vb',
  '-4px',
  '+4px',
  '.5px',
  '1e2px',
];

// A length is a number with a unit. Everything here fails that on its first
// character or its last, and each is a value the scan must keep reading as
// "not an offset" -- a colour in the offset slot is how a hand-drawn glow used
// to pass, so admitting one here would be the expensive direction.
const NOT_LENGTHS = ['red', '#fff', 'var(--mint)', 'calc(1px + 1px)', 'rgba(0,0,0,.5)', 'thick', '', '1px2', 'px'];

describe('isLength', () => {
  it.each(LENGTHS)('reads %s as a length', (part) => {
    expect(isLength(part)).toBe(true);
  });

  it.each(NOT_LENGTHS)('does not read %s as a length', (part) => {
    expect(isLength(part)).toBe(false);
  });

  // The property, stated so a future narrowing fails here rather than in a
  // review round: the unit is any identifier, never a list of them. A pattern
  // that spells `px|rem` has gone back to enumerating what CSS leaves open.
  it('matches the unit as a grammar rather than a list', () => {
    const source = fs.readFileSync(new URL('cssLength.mjs', import.meta.url), 'utf8');
    const pattern = source.match(/^const LENGTH = (?<pattern>.+);$/m);

    expect(pattern, 'LENGTH is no longer spelled that way').not.toBeNull();
    expect(pattern.groups.pattern).not.toMatch(/\bpx\b/);

    // Whatever it is spelled as, it accepts a unit nobody has heard of. This is
    // the assertion the case list above cannot make about itself.
    expect(isLength('7newunit')).toBe(true);
  });
});

describe('isZeroLength', () => {
  // Every spelling of zero is zero. Reading only the literal `0` let
  // `-0px 0 93px red` take the exemption that exists for directional light.
  it.each(['0', '0px', '-0px', '+0px', '0.0', '00px', '0vw', '0cm'])('reads %s as zero', (part) => {
    expect(isZeroLength(part)).toBe(true);
  });

  it.each(['1px', '-4px', '.5rem', '0.1cm'])('does not read %s as zero', (part) => {
    expect(isZeroLength(part)).toBe(false);
  });
});
