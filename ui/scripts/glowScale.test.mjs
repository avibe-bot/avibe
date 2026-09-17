import fs from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import postcss from 'postcss';

import { customPropertiesIn } from './customProperties.mjs';
import { intendedFiles } from './lintPolicy.mjs';
import { typeScriptComments } from './nonRenderingText.mjs';
import { GLOW_TOKEN } from './shadowLayer.mjs';
import { eachStylesheet } from './stylesheets.mjs';
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
const SRC_ROOT = fileURLToPath(new URL('../src/', import.meta.url));
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
    holds: /^0 0 \d+px -4px rgba\(\d+, \d+, \d+, 0\.6\)$/,
  },
  // The approved onboarding reference draws its active card with no spread at all
  // and at #5BFFA060, so the two fields the general rule fixes -- spread = -blur/4
  // and alpha 0.44 -- are the two this role is excused from, and `holds` pins both
  // at the reference's own numbers. Its blur is not excused: it is asserted from
  // ROLE_BLUR like every other role's, because a role is still a name for a size.
  //
  // Two patterns, because the two frames drew this card separately: Light's own
  // (waUhu) is 16/-4 at #10B98170, so the fields excused here are the ones that
  // differ. An exception that named one theme would have left the other
  // unasserted, which is this file's recurring failure shape -- so a themed
  // exception states BOTH themes, and a theme it says nothing about fails rather
  // than passing by default.
  onboarding: {
    why: 'the owner-approved active-card halo: spreadless #5BFFA060 in dark, and the light frame\'s own 16/-4 #10B98170',
    holds: {
      dark: /^0 0 \d+px 0px color-mix\(in srgb, var\(--[a-z]+\) 37\.6%, transparent\)$/,
      light: /^0 0 \d+px -4px color-mix\(in srgb, var\(--[a-z]+\) 44%, transparent\)$/,
    },
  },
};

// What each role's blur IS, rather than which blurs the scale happens to
// contain. A set says `md` and `lg` are both spellable while saying nothing
// about which is 24 and which is 32, so swapping two rungs' blurs left every
// assertion here green and every converted call site one rung off its frame.
// A role is a name for a size; the mapping is the thing being asserted.
// A role whose size is themed names one size per theme instead of one size. That is
// not a softening: a single number still means "this blur in every theme", and a
// themed role must state every theme or `blurOf` returns undefined and the assertion
// fails. What it is NOT is an escape -- `onboarding` is pinned to 28 and 16, not
// excused from having a blur, and `cta` is pinned to 16 and 20 where it used to be
// excused entirely: "themed" was being read as "unasserted", so the owner's two
// numbers were the only blurs in this file that nothing checked.
const ROLE_BLUR = {
  dot: 8, wire: 4, xs: 12, sm: 16, md: 24, lg: 32, xl: 48,
  cta: { dark: 16, light: 20 },
  onboarding: { dark: 28, light: 16 },
};

const blurOf = (role, theme) => {
  const blur = ROLE_BLUR[role];
  return typeof blur === 'number' ? blur : blur?.[theme];
};

// Every rung is read under both palettes, not under the block it is written in.
// That block is always `@theme`: the token layer is `@theme inline`, so Tailwind
// substitutes each value into its utilities at build time and `validate:theme`
// fails a later re-declaration of the same name as dead. Asking which theme a
// DECLARATION belongs to therefore has one answer for every glow in the tree, and
// the question worth asking is what the declaration draws once each palette has
// supplied its own numbers.
const THEMES = ['dark', 'light'];

// Which is how a glow is themed at all, since a value cannot be. A hue follows the
// palette on its own -- `color-mix(in srgb, var(--mint) …)` is whichever mint the
// theme declares -- but a blur is a number, and a number that has to change per
// theme has to be named and re-anchored where the palette is. `--brand-glow-blur`
// is that shape and `--onboarding-glow-*` follows it.
//
// So a rung is resolved through the numbers its theme declares before anything is
// asserted about it. Only the numbers: a colour name is left as written, because
// "this token's colour is that accent" is an assertion below and resolving it away
// would delete it. The whole-selector matching is the one `DARK_ACCENTS` explains
// -- a substring test reads `:root:not([data-theme="dark"])`, the light block whose
// entire job is to say it is not the dark one, as a dark declaration.
const NUMBER = /^-?[\d.]+(px|%)?$/;

const THEME_NUMBERS = (() => {
  const numbers = { dark: new Map(), light: new Map() };
  postcss.parse(CSS).walkRules((rule) => {
    let light = rule.selectors.some((one) => one.trim() === '[data-theme="light"]');
    for (let at = rule.parent; at && !light; at = at.parent) {
      light = at.type === 'atrule' && at.name === 'media' && /prefers-color-scheme:\s*light/.test(at.params);
    }
    const dark = !light && rule.selectors.some((one) => [':root', '[data-theme="dark"]'].includes(one.trim()));
    if (!light && !dark) return;

    for (const node of rule.nodes ?? []) {
      if (node.type !== 'decl' || !node.prop.startsWith('--') || !NUMBER.test(node.value.trim())) continue;
      numbers[light ? 'light' : 'dark'].set(node.prop, node.value.trim());
    }
  });

  // Light declares only what it changes, so it reads through dark for the rest --
  // the cascade, which is what the browser does with these two blocks.
  return { dark: numbers.dark, light: new Map([...numbers.dark, ...numbers.light]) };
})();

const resolveNumbers = (value, theme) => {
  let resolved = value.replace(/\s+/g, ' ').trim();
  for (let hop = 0; hop < 4; hop += 1) {
    const next = resolved.replace(/var\((--[\w-]+)\)/g, (written, name) =>
      THEME_NUMBERS[theme].get(name) ?? written);
    if (next === resolved) break;
    resolved = next;
  }
  return resolved;
};

const SPREAD_CAP = 12;
const GLOW_ALPHA = 0.44;

// Over every stylesheet the validator reads, not `src/index.css` alone, and
// with a parser rather than a line-anchored regex. Both halves of that were the
// same gap: `collectTokenLayer()` sanctions `@theme` declarations from every
// scanned stylesheet, so a component stylesheet could declare
// `@theme { --shadow-glow-rogue-mint: 0 0 93px red }`, have a call site consume
// it through `var(…)`, and have runtime validation trust it as managed while
// nothing here ever checked its name, geometry, colour or role. The domain is
// shared with the validator now (`eachStylesheet`), so the two cannot disagree
// about which stylesheets exist any more than they can about what a managed
// name is.
const SHEETS = [...eachStylesheet(SRC_ROOT)];

const RUNG_NAME = /^--shadow-glow-(?<role>[a-z]+)-(?<accent>[a-z]+)$/;

const rungs = SHEETS.flatMap(([, sheet]) => {
  const found = [];
  sheet.walkDecls((decl) => {
    const match = RUNG_NAME.exec(decl.prop);
    // Every DECLARATION under every theme, not every name: reading one per name
    // would leave whichever came second unchecked -- the same "collected, marked
    // managed and discarded unread" hole the validator's own token layer had -- and
    // reading one theme per declaration leaves the other palette's numbers
    // unasserted, which is the same hole one level along.
    if (!match) return;
    for (const theme of THEMES) {
      found.push({ ...match.groups, theme, token: decl.prop, value: resolveNumbers(decl.value, theme) });
    }
  });
  return found;
});

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
//
// Which is why the widening is not another regex. A hand-written one closed the
// role-and-accent gap and opened a smaller one in the same shape:
// `--shadow-glow-[a-z0-9-]+` omits `_`, a character CSS allows in a custom
// property and the validator sanctions without noticing, so
// `--shadow-glow-rogue_name: 0 0 93px red` was again absent from every assertion
// here while `shadow-[var(--shadow-glow-rogue_name)]` passed `validate:theme`.
// The set is therefore read the way the validator reads it -- `@theme` walked
// with a parser, names matched with the validator's OWN pattern -- so the two
// cannot disagree about what a managed name is, whatever it is spelled with.
const MANAGED = SHEETS.flatMap(([, sheet]) => {
  const names = [];
  sheet.walkAtRules('theme', (rule) => {
    rule.walkDecls((decl) => {
      if (GLOW_TOKEN.test(decl.prop)) names.push(decl.prop);
    });
  });
  return names;
});

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

  // And the role a name parses into has to be one the scale defines. A role is a
  // name for a size; a name for no size is not a role.
  it.each(rungs)('$token in $theme names a role the scale defines', ({ role }) => {
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
  it.each(rungs)('$token in $theme is its accent, as the dark theme declares it', ({ accent, value }) => {
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
  //
  // Every role, with no branch for a themed one. That branch read "then it must
  // carry a themed blur", which described `cta` exactly -- and so accepted any
  // number at all behind the variable, including a role invented that morning.
  // Resolving a rung through its theme's own numbers is what removes the need for
  // it: a themed blur is a number by the time it gets here.
  it.each(rungs)('$token has its role\'s blur in $theme', ({ role, theme, value }) => {
    const blur = blurOf(role, theme);
    expect(blur, `${role} names no blur for the ${theme} theme it is declared in`).toBeDefined();
    expect(Number(value.match(/^0 0 (\d+)px/)?.[1])).toBe(blur);
  });

  it.each(Object.entries(OFF_RULE))('states why %s is off the rule', (role, { why }) => {
    expect(why.length).toBeGreaterThan(0);
    expect(rungs.some((rung) => rung.role === role)).toBe(true);
  });

  // Every accent's token of an off-rule role, so a role is covered across the
  // palette rather than at whichever accent happens to be first.
  it.each(rungs.filter((rung) => rung.role in OFF_RULE))('$token holds its documented exception in $theme', ({ role, theme, value }) => {
    const { holds, why } = OFF_RULE[role];
    const pattern = holds instanceof RegExp ? holds : holds[theme];
    expect(pattern, `${role} states no exception for the ${theme} theme it is declared in`).toBeDefined();
    expect(value.trim(), why).toMatch(pattern);
  });

  // Which comments count as design glow annotations, and what each one measures.
  //
  // A wash is `0 y<n>px`, and the card scale owns those; this scale is only ever
  // centred, so an annotation naming a y-offset is not its. The offset can be
  // written before or after the blur -- welCard puts it after -- so the whole
  // comment is the unit, not a window around the word, which is why these are
  // parsed rather than grepped.
  //
  // `blur` also names things that are not shadows -- a backdrop filter, a
  // privacy blur on a screenshot -- and reading `blur 10` out of one of those
  // would fail this suite over a number no shadow ever wanted. So the comment
  // must also name what it is measuring. A sigil (`@design-glow`) would be
  // sharper, but only until the annotation that forgets to carry it: that turns
  // this false positive into a silent miss, which is the worse direction.
  // Naming the shadow is what a design annotation already does -- every
  // contributing one in this tree says "glow" or "shadow" -- so the marker
  // maintains itself.
  //
  // A function rather than three lines inside the loop, because the whole-tree
  // test can only ever report that some number leaked in; it cannot say which
  // comment shape let it. The probes below say that.
  const ANNOTATED_BLUR = (comment) => {
    const blur = comment.match(/\bblur (?<blur>\d+)/);
    if (!blur) return null;
    if (!/\b(glow|shadow)\b/i.test(comment)) return null;
    if (/\by\d/.test(comment)) return null;
    return Number(blur.groups.blur);
  };

  it.each([
    ['a glow annotation', '// diagPulse: mint glow, blur 16', 16],
    ['a shadow annotation', '/* StepCard shadow at blur 48 */', 48],
    ['either word cased as prose', '// Glow: blur 24', 24],
    ['a backdrop filter', '// the sheet sits over a backdrop blur 10 panel', null],
    ['a privacy blur', '// screenshots ship with the avatar under blur 12', null],
    ['a wash, which the card scale owns', '// welCard: glow 0 y4px blur 64', null],
    ['a comment naming no blur at all', '// mint glow at #5BFFA070', null],
  ])('reads %s', (_, comment, expected) => {
    expect(ANNOTATED_BLUR(comment)).toBe(expected);
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
        const blur = ANNOTATED_BLUR(comment);
        if (blur !== null) annotated.add(blur);
      }
    }

    expect(annotated.size).toBeGreaterThan(0);

    // Read off ROLE_BLUR rather than off the declarations, so this asks the
    // scale the test pins and not whatever index.css currently happens to say.
    // Taking it from the file made the two agree by construction: a rung nudged
    // to 40px became "spellable" in the same edit that broke its frame.
    const spellable = new Set(Object.values(ROLE_BLUR)
      .flatMap((blur) => (typeof blur === 'number' ? [blur] : Object.values(blur))));

    expect([...annotated].filter((blur) => !spellable.has(blur)).sort((a, b) => a - b)).toEqual([]);
  }, WHOLE_TREE_SCAN);
});
