import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { intendedFiles } from './lintPolicy.mjs';
import { typeScriptComments } from './nonRenderingText.mjs';

// `validate:theme` already forces every glow in the tree to be a
// `--shadow-glow-*` token. That check is about spelling: it says a call site
// may not invent a value. It says nothing about whether the values it points at
// are the design's.
//
// The first cut of this scale got that wrong in a way the guard could not see.
// Each rung was set to roughly the middle of the literals it had to absorb, so
// `sm` came out at spread -2px -- a number that appears nowhere in design.pen
// and nowhere in this tree, where every 16px glow was already -4px. Every
// converted site then moved off its frame to reach it, and the guard passed,
// because a token was a token.
//
// design.pen does not carry three loose triples. It draws one shape: a glow is
// centred, its spread is a quarter of its blur, and its colour is the accent at
// #5BFFA070. diagPulse (16/-4), heroPulse (24/-6), diagHero (32/-8) and
// StepCard (48/-12) all agree, and welCard at blur 64 holds -12, so -12 is the
// cap rather than a fifth data point.
//
// So this asserts the rule, not the current numbers. A rung added later is
// covered without editing the test, and a rung nudged off the rule fails here
// instead of shipping.

const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));
const CSS = fs.readFileSync(new URL('../src/index.css', import.meta.url), 'utf8');

// The roles that are deliberately not on the rule, each with the reason it is
// off. This is a closed set -- the roles that exist -- not a list of exceptions
// that grows when a value is inconvenient. A new role is on the rule unless it
// is added here with a reason.
//
// A reason is not an assertion, and for three rounds this was only a reason.
// Naming a role here removed it from every value check above, so the exception
// meant "unchecked" rather than "checked differently": a wire token could be
// changed to `0 0 32px -8px red`, a dot could grow a spread, the CTA alpha could
// leave the value the owner set, and the suite stayed green -- and
// `validate-theme.mjs` trusts these declarations too, so nothing else would have
// caught it either. Each exception now carries the contract it is an exception
// TO, so the escape from the general rule is itself a rule.
const OFF_RULE = {
  dot: {
    why: 'a spreadless status dot, clamped to 0.9 because ours sit on lit panels',
    holds: /^0 0 \d+px rgba\(\d+, \d+, \d+, 0\.9\)$/,
  },
  wire: {
    why: 'a drop-shadow() filter, which takes no spread at all',
    holds: /^0 0 \d+px color-mix\(in srgb, var\(--[a-z]+\) \d+%, transparent\)$/,
  },
  cta: {
    why: 'owner-set: themed blur (2026-08-14) and 0.6 alpha, not from design.pen',
    holds: /^0 0 var\(--brand-glow-blur\) -4px rgba\(\d+, \d+, \d+, 0\.6\)$/,
  },
};

const SPREAD_CAP = 12;
const GLOW_ALPHA = 0.44;

const rungs = [...CSS.matchAll(/^\s*--shadow-glow-(?<role>[a-z]+)-(?<accent>[a-z]+):\s*(?<value>[^;]+);/gm)].map(
  (match) => ({ ...match.groups, token: `--shadow-glow-${match.groups.role}-${match.groups.accent}` })
);

const sized = rungs.filter((rung) => !(rung.role in OFF_RULE));

describe('the accent glow scale', () => {
  it('has rungs to check', () => {
    // Guards the two greps below: a regex that silently matched nothing would
    // make every assertion here vacuously true.
    expect(sized.length).toBeGreaterThan(0);
  });

  it.each(sized)('$token is centred with spread = -blur/4', ({ value }) => {
    const geometry = value.match(/^0 0 (?<blur>\d+)px (?<spread>-\d+)px /);
    expect(geometry, `${value} is not "0 0 <blur>px -<spread>px <colour>"`).not.toBeNull();

    const blur = Number(geometry.groups.blur);
    expect(Number(geometry.groups.spread)).toBe(-Math.min(SPREAD_CAP, blur / 4));
  });

  it.each(sized)('$token carries the design alpha', ({ value }) => {
    const alpha = value.match(/rgba\([^)]*,\s*([\d.]+)\)/);
    expect(alpha, `${value} is not an rgba() literal`).not.toBeNull();
    expect(Number(alpha[1])).toBe(GLOW_ALPHA);
  });

  it.each(Object.entries(OFF_RULE))('states why %s is off the rule', (role, { why }) => {
    expect(why.length).toBeGreaterThan(0);
    expect(rungs.some((rung) => rung.role === role)).toBe(true);
  });

  // Every accent's token of an off-rule role, so a role is covered across the
  // palette rather than at whichever accent happens to be first.
  it.each(rungs.filter((rung) => rung.role in OFF_RULE))('$token holds its documented exception', ({ role, value }) => {
    expect(value.trim(), OFF_RULE[role].why).toMatch(OFF_RULE[role].holds);
  });

  // The rule fixes the shape of a rung; it does not decide which blurs exist.
  // That is design.pen's, and the annotations are how it reaches the source: a
  // component that names `blur 24` and then renders a 32px glow has been
  // redesigned by a codemod. Checking that the scale can SPELL each annotated
  // blur is the half that holds without guessing which element in a file an
  // annotation refers to -- welCard's does not describe the shadow nearest it.
  it('can spell every blur a design annotation names', () => {
    const annotated = new Set();

    for (const relative of intendedFiles(UI_ROOT, { extensions: ['.ts', '.tsx'] })) {
      const source = fs.readFileSync(new URL(relative, new URL(UI_ROOT, 'file:')), 'utf8');
      for (const comment of typeScriptComments(source, relative)) {
        const blur = comment.match(/\bblur (?<blur>\d+)/);
        // A wash is `0 y<n>px`, and the card scale owns those; this scale is
        // only ever centred, so an annotation naming a y-offset is not its. The
        // offset can be written before or after the blur -- welCard puts it
        // after -- so the whole comment is the unit, not a window around the
        // word, which is why these are parsed rather than grepped.
        if (blur && !/\by\d/.test(comment)) annotated.add(Number(blur.groups.blur));
      }
    }

    expect(annotated.size).toBeGreaterThan(0);

    const spellable = new Set(sized.map(({ value }) => Number(value.match(/^0 0 (\d+)px/)[1])));
    for (const role of Object.keys(OFF_RULE)) {
      for (const rung of rungs.filter((entry) => entry.role === role)) {
        const blur = rung.value.match(/^0 0 (\d+)px/);
        if (blur) spellable.add(Number(blur[1]));
      }
    }

    expect([...annotated].filter((blur) => !spellable.has(blur)).sort((a, b) => a - b)).toEqual([]);
  });
});
