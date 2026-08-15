import { describe, expect, it } from 'vitest';

import { declarationSpansIn, declaresAt } from './cssDeclarations.mjs';

// A property name followed by a colon is the SPELLING of a declaration, and the
// scan that reads these files treated it as one wherever it appeared. Two things
// wear that spelling and declare nothing: a pseudo-class on a class named after
// a property, and any sentence that quotes CSS. The first failed a stylesheet
// that drew no shadow at all -- `validate:theme` runs on every change, so that
// is a correct pull request blocked by a guard.
//
// These cases are the property in both directions, because only stating both
// keeps either honest: a position CSS declares at must be found, and a position
// that merely looks like one must not -- and answering "no" everywhere would
// silence the whole guard while every false-positive case below still passed.

// The offset of a needle, so a case reads as "this spelling, at this place"
// rather than as arithmetic.
const at = (css, needle) => declaresAt(declarationSpansIn(css), css.indexOf(needle));

describe('where a stylesheet declares', () => {
  it.each([
    ['a declaration in a rule', '.a { box-shadow: 0 0 4px red }', 'box-shadow'],
    ['a custom property', ':root { --shadow-glow-x: 0 0 4px red }', '--shadow-glow-x'],
    // `@theme` is where this tree's tokens live, so a declaration inside an
    // at-rule block has to count exactly as one in a plain rule does.
    ['a declaration inside an at-rule', '@theme { --shadow-glow-x: 0 0 4px red }', '--shadow-glow-x'],
    ['a declaration nested under a media query', '@media (min-width: 1px) { .a { box-shadow: 0 0 4px red } }', 'box-shadow'],
    // A bare declaration list is what an inline `style` attribute and
    // `.style.cssText` both carry: CSS with no rule around it.
    ['a bare declaration list', 'box-shadow: 0 0 4px red', 'box-shadow'],
    ['the second of two on one line', '.a { color: red; box-shadow: 0 0 4px red }', 'box-shadow'],
  ])('declares at %s', (_label, css, needle) => expect(at(css, needle)).toBe(true));

  it.each([
    // The reported case: a class named after a property, and the colon that
    // follows it is a pseudo-class.
    ['a pseudo-class on a property-named class', '.box-shadow:hover { color: red }', 'box-shadow'],
    ['the same name in a plain selector', '.box-shadow { color: red }', 'box-shadow'],
    ['an attribute selector', '[data-x="box-shadow: 0 0 4px red"] { color: red }', 'box-shadow'],
    // A condition NAMES a value in order to test it; nothing is applied.
    ['a feature query condition', '@supports (box-shadow: 0 0 4px red) { .a { color: red } }', 'box-shadow'],
    ['a comment', '/* box-shadow: 0 0 4px red */\n.a { color: red }', 'box-shadow'],
  ])('does not declare at %s', (_label, css, needle) => expect(at(css, needle)).toBe(false));
});

// Which direction being wrong points in. This module exists to PROVE a match is
// not a declaration, so text no parser can read proves nothing -- and the caller
// has to be left exactly as suspicious of it as it was before this file existed,
// rather than let an unreadable stretch of CSS become a way past the guard.
describe('text no parser can read', () => {
  it('keeps its whole range scannable', () => {
    const broken = '.a { box-shadow: 0 0 4px red';
    expect(declarationSpansIn(broken)).toEqual([[0, broken.length]]);
    expect(at(broken, 'box-shadow')).toBe(true);
  });

  // Not a span of length zero, which `declaresAt` would answer "no" to for the
  // right reason and the wrong one at once.
  it('claims nothing when there is nothing to claim', () => {
    expect(declarationSpansIn('')).toEqual([]);
  });
});

// Offsets, not values, because the caller is a byte scan that has already
// matched -- and a caller folding several stretches of one file into one list
// needs them in the coordinates of the FILE, not of the stretch. A `<style>`
// child is CSS whose offsets are its position in a `.tsx` file.
describe('offsets a caller can fold', () => {
  it('reports them in the containing file coordinates', () => {
    const file = 'const css = ".a { box-shadow: 0 0 4px red }";';
    const start = file.indexOf('.a');
    const spans = declarationSpansIn(file.slice(start, file.lastIndexOf('"')), start);

    expect(spans).toHaveLength(1);
    expect(file.slice(...spans[0])).toBe('box-shadow: 0 0 4px red');
  });

  it('accumulates into one list across stretches', () => {
    const spans = declarationSpansIn('color: red', 0);
    declarationSpansIn('box-shadow: 0 0 4px red', 100, spans);

    expect(spans).toHaveLength(2);
    expect(declaresAt(spans, 100)).toBe(true);
    expect(declaresAt(spans, 50)).toBe(false);
  });
});
