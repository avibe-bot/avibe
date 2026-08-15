import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import postcss from 'postcss';

import { customPropertiesIn } from './customProperties.mjs';
import { intendedFiles } from './lintPolicy.mjs';
import { typeScriptComments } from './nonRenderingText.mjs';
import { WHOLE_TREE_SCAN } from './wholeTreeScan.mjs';

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
// `holds` pins every number the general rule would have pinned. Leaving `\d+`
// where the rule would have computed a value is the exception eating one field
// wider than it was granted: `wire` is off the rule because `drop-shadow()`
// takes no SPREAD, which says nothing about its blur, and a `\d+px` there let
// the 4px design.pen draws become 40px with the suite green. An exception names
// the field it excuses and fixes the rest.
const OFF_RULE = {
  dot: {
    why: 'a spreadless status dot at blur 8, clamped to 0.9 because ours sit on lit panels',
    holds: /^0 0 8px rgba\(\d+, \d+, \d+, 0\.9\)$/,
  },
  wire: {
    why: 'a drop-shadow() filter at blur 4, which takes no spread at all',
    holds: /^0 0 4px color-mix\(in srgb, var\(--[a-z]+\) 40%, transparent\)$/,
  },
  cta: {
    why: 'owner-set: themed blur (2026-08-14) and 0.6 alpha, not from design.pen',
    holds: /^0 0 var\(--brand-glow-blur\) -4px rgba\(\d+, \d+, \d+, 0\.6\)$/,
  },
};

// What each role's blur IS, rather than which blurs the scale happens to
// contain. A set says `md` and `lg` are both spellable while saying nothing
// about which is 24 and which is 32, so swapping two rungs' blurs left every
// assertion here green and every converted call site one rung off its frame.
// A role is a name for a size; the mapping is the thing being asserted.
const ROLE_BLUR = {
  dot: 8, wire: 4, xs: 12, sm: 16, md: 24, lg: 32, xl: 48,
};

const SPREAD_CAP = 12;
const GLOW_ALPHA = 0.44;

const rungs = [...CSS.matchAll(/^\s*--shadow-glow-(?<role>[a-z]+)-(?<accent>[a-z]+):\s*(?<value>[^;]+);/gm)].map(
  (match) => ({ ...match.groups, token: `--shadow-glow-${match.groups.role}-${match.groups.accent}` })
);

// Every name the runtime validator will sanction, which is a wider set than the
// one above can read. `validate:theme` accepts any `--shadow-glow-*` declared
// in `@theme` -- managed is a PLACE -- while `rungs` requires a role AND an
// accent, so `--shadow-glow-rogue: 0 0 93px red` was silently absent from every
// assertion in this file and `shadow-[var(--shadow-glow-rogue)]` passed both
// guards carrying geometry from nowhere.
//
// That gap is the enumeration failure this file's own header warns about, one
// level up: the rules below are stated as properties, but they were applied to
// whichever declarations a regex happened to match. A grammar that skips what
// it cannot parse reports a clean scale by not looking at the exception.
const MANAGED = [...CSS.matchAll(/^\s*(?<token>--shadow-glow-[a-z0-9-]+):/gm)].map(
  (match) => match.groups.token
);

const sized = rungs.filter((rung) => !(rung.role in OFF_RULE));

// The accent values as the dark theme declares them, which is what these tokens
// carry. `@theme inline` is not themed, so a glow cannot route through
// `var(--mint)` and follow the palette the way `--card-wash` does -- the RGB is
// written out, and a written-out RGB is a copy that can go stale. Reading the
// source it was copied FROM is what turns the copy back into a derivation.
// The selector is matched WHOLE, per comma-separated part. A substring test
// reads `:root:not([data-theme="dark"])` -- the light block, whose entire job is
// to say it is not the dark one -- as a dark declaration, and then every accent
// has two conflicting values and no assertion can be made about either. That is
// the same mistake this scan's own history is made of: a structural question
// answered by looking for characters.
const DARK_ACCENTS = (() => {
  const declared = new Map();
  postcss.parse(CSS).walkRules((rule) => {
    if (rule.selectors.some((one) => one.trim() === '[data-theme="dark"]')) {
      customPropertiesIn(rule, declared);
    }
  });
  return declared;
})();

// `#5bffa0` as `91, 255, 160` -- the one spelling difference between an accent
// and the glow that carries it.
const channels = (hex) => {
  const digits = hex.trim().replace(/^#/, '');
  if (!/^[0-9a-f]{6}$/i.test(digits)) return null;
  return [0, 2, 4].map((at) => parseInt(digits.slice(at, at + 2), 16)).join(', ');
};

describe('the accent glow scale', () => {
  it('has rungs to check', () => {
    // Guards the two greps below: a regex that silently matched nothing would
    // make every assertion here vacuously true.
    expect(sized.length).toBeGreaterThan(0);
  });

  // The bridge between what the validator manages and what this file checks.
  // Without it, a name off the grammar is not a failing rung -- it is no rung at
  // all, and every `it.each` below simply never runs for it.
  it('reads every managed glow name as a rung', () => {
    expect(MANAGED.length).toBeGreaterThan(0);
    expect(MANAGED.filter((token) => !rungs.some((rung) => rung.token === token))).toEqual([]);
  });

  // And the role a name parses into has to be one the scale defines. `role in
  // ROLE_BLUR` already decides which blur is asserted, but its else-branch --
  // "then it must carry a themed blur" -- describes `cta`, so an invented role
  // spelled with `var(--…)` would satisfy it. A role is a name for a size; a
  // name for no size is not a role.
  it.each(rungs)('$token names a role the scale defines', ({ role }) => {
    expect(role in ROLE_BLUR || role in OFF_RULE, `${role} is on neither the blur scale nor the off-rule list`).toBe(true);
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

  // Every rung, not just the sized ones: `dot` and `cta` pin their alpha in
  // `holds` and leave the RGB as `\d+, \d+, \d+`, so before this the colour of a
  // status dot was unasserted in every theme. A token named for an accent that
  // draws a different one is the same defect as a blur off its frame, and it is
  // the harder one to see by eye.
  it.each(rungs)('$token is its accent, as the dark theme declares it', ({ accent, value }) => {
    const declared = DARK_ACCENTS.get(`--${accent}`);
    expect(declared, `--${accent} is declared in no [data-theme="dark"] block`).toBeDefined();
    expect([...declared], `--${accent} is declared more than once in dark`).toHaveLength(1);

    // Two spellings, because `wire` mixes the live variable while the rest write
    // the channels out. Both are "this token's colour is that accent"; only the
    // second can drift, and asserting them apart is what let it.
    const mixed = value.match(/color-mix\(in srgb, var\((--[a-z]+)\)/);
    if (mixed) {
      expect(mixed[1]).toBe(`--${accent}`);
      return;
    }

    const written = value.match(/rgba\((\d+, \d+, \d+),/);
    expect(written, `${value} is neither a color-mix() nor an rgba() literal`).not.toBeNull();
    expect(written[1]).toBe(channels([...declared][0]));
  });

  // A role is a name for a size, so the size is the assertion. Membership in a
  // set of blurs cannot see two roles trading values.
  it.each(rungs)('$token has its role\'s blur', ({ role, value }) => {
    if (!(role in ROLE_BLUR)) {
      expect(value, `${role} has no fixed blur, so it must carry a themed one`).toMatch(/^0 0 var\(--[a-z-]+\) /);
      return;
    }
    expect(Number(value.match(/^0 0 (\d+)px/)?.[1])).toBe(ROLE_BLUR[role]);
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

    // Read off ROLE_BLUR rather than off the declarations, so this asks the
    // scale the test pins and not whatever index.css currently happens to say.
    // Taking it from the file made the two agree by construction: a rung nudged
    // to 40px became "spellable" in the same edit that broke its frame.
    const spellable = new Set(Object.values(ROLE_BLUR));

    expect([...annotated].filter((blur) => !spellable.has(blur)).sort((a, b) => a - b)).toEqual([]);
  }, WHOLE_TREE_SCAN);
});
