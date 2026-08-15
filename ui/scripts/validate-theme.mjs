import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import postcss from 'postcss';

import {
  COLOUR,
  glowOffencesInValue,
  readsIntoDropShadow,
  spreadOffencesInDropShadow,
} from './shadowLayer.mjs';
import { SHADOW_KEY, isStyleWrite, propertyExpression, valueArgument } from './styleWrite.mjs';
import { intendedFiles } from './lintPolicy.mjs';
import { colourRegistrationsIn, customPropertiesIn } from './customProperties.mjs';
import { declarationSpansIn, declaresAt } from './cssDeclarations.mjs';
import {
  cssRangesIn,
  parseSource,
  rendersAtAll,
  withoutNonRenderingText,
} from './nonRenderingText.mjs';

const html = fs.readFileSync('index.html', 'utf8');
const css = fs.readFileSync('src/index.css', 'utf8');
// The Model Hub carries its own theme-token layer (design.pen's dark frame plus
// light overrides), so it has to satisfy the same cascade contract as index.css.
const modelHubCss = fs.readFileSync('src/components/settings/models/modelHubSurface.css', 'utf8');

function extractThemeBootstrap() {
  const match = html.match(/<script>\r?\n([\s\S]*?)\r?\n\s*<\/script>/);
  if (!match) {
    throw new Error('Theme bootstrap script was not found in index.html');
  }
  return match[1];
}

function runBootstrap({ search = '', stored = null }) {
  const attrs = {};
  const context = {
    URLSearchParams,
    window: {
      location: { search },
      localStorage: { getItem: () => stored },
    },
    document: {
      documentElement: {
        setAttribute: (name, value) => {
          attrs[name] = value;
        },
      },
    },
  };

  vm.runInNewContext(extractThemeBootstrap(), context);
  return attrs['data-theme'] ?? null;
}

function assertEqual(name, actual, expected) {
  if (actual !== expected) {
    throw new Error(`${name}: expected ${expected ?? 'system-css'}, got ${actual ?? 'system-css'}`);
  }
}

function normalizeCssValue(value) {
  return value.replace(/\s+/g, ' ').trim();
}

function mediaApplies(rule, prefersLight) {
  let node = rule.parent;
  while (node) {
    if (node.type === 'atrule' && node.name === 'media') {
      if (node.params === '(prefers-color-scheme: light)' && !prefersLight) {
        return false;
      }
    }
    node = node.parent;
  }

  return true;
}

function selectorMatches(selector, themeAttr) {
  switch (selector.trim()) {
    case ':root':
      return true;
    case '[data-theme="dark"]':
      return themeAttr === 'dark';
    case '[data-theme="light"]':
      return themeAttr === 'light';
    case ':root:not([data-theme="dark"])':
      return themeAttr !== 'dark';
    default:
      return false;
  }
}

function selectorSpecificity(selector) {
  const idCount = (selector.match(/#/g) ?? []).length;
  const classLikeCount = (selector.match(/(\.|:|\[)/g) ?? []).length;
  const elementCount = selector.replace(/#[\w-]+|[.][\w-]+|:[\w-]+(?:\([^)]*\))?|\[[^\]]+\]/g, '').trim()
    ? 1
    : 0;
  return idCount * 100 + classLikeCount * 10 + elementCount;
}

function splitSelectors(selectorText) {
  return selectorText.split(',').map((selector) => selector.trim());
}

function resolveThemeTokens({ prefersLight, themeAttr, source = css }) {
  const root = postcss.parse(source);
  const resolved = new Map();
  let order = 0;

  root.walkRules((rule) => {
    if (!mediaApplies(rule, prefersLight)) {
      return;
    }

    const matchingSpecificity = splitSelectors(rule.selector)
      .filter((selector) => selectorMatches(selector, themeAttr))
      .reduce((highest, selector) => Math.max(highest, selectorSpecificity(selector)), -1);

    if (matchingSpecificity === -1) {
      return;
    }

    rule.walkDecls((decl) => {
      if (decl.prop !== 'color-scheme' && !decl.prop.startsWith('--')) {
        return;
      }

      const previous = resolved.get(decl.prop);
      if (!previous || matchingSpecificity > previous.specificity || (matchingSpecificity === previous.specificity && order > previous.order)) {
        resolved.set(decl.prop, {
          order,
          specificity: matchingSpecificity,
          value: normalizeCssValue(decl.value),
        });
      }
    });

    order += 1;
  });

  return new Map([...resolved.entries()].map(([key, entry]) => [key, entry.value]));
}

function assertTokenMapsEqual(name, actual, expected) {
  const actualKeys = [...actual.keys()].sort();
  const expectedKeys = [...expected.keys()].sort();
  assertEqual(`${name} token count`, actualKeys.length, expectedKeys.length);

  for (const key of expectedKeys) {
    assertEqual(`${name} ${key}`, actual.get(key), expected.get(key));
  }
}

const bootstrapCases = [
  ['first visit leaves system to CSS', runBootstrap({}), null],
  ['stored system leaves system to CSS', runBootstrap({ stored: 'system' }), null],
  ['stored light restores explicit override', runBootstrap({ stored: 'light' }), 'light'],
  ['stored dark restores explicit override', runBootstrap({ stored: 'dark' }), 'dark'],
  ['query system clears stored dark override', runBootstrap({ search: '?theme=system', stored: 'dark' }), null],
  ['query light wins over stored dark', runBootstrap({ search: '?theme=light', stored: 'dark' }), 'light'],
  ['invalid stored value leaves system to CSS', runBootstrap({ stored: 'sepia' }), null],
];

for (const [name, actual, expected] of bootstrapCases) {
  assertEqual(name, actual, expected);
}

const systemDark = resolveThemeTokens({ prefersLight: false, themeAttr: null });
const systemLight = resolveThemeTokens({ prefersLight: true, themeAttr: null });
const explicitDark = resolveThemeTokens({ prefersLight: true, themeAttr: 'dark' });
const explicitLight = resolveThemeTokens({ prefersLight: false, themeAttr: 'light' });

assertTokenMapsEqual('system light and explicit light cascade', systemLight, explicitLight);
assertTokenMapsEqual('system dark and explicit dark cascade', systemDark, explicitDark);
assertEqual('system light background', systemLight.get('--background'), '#f4f6fb');
assertEqual('system dark background', systemDark.get('--background'), '#080812');
assertEqual('system light color-scheme', systemLight.get('color-scheme'), 'light');
assertEqual('system dark color-scheme', systemDark.get('color-scheme'), 'dark');

// Each accent is a fill (--X, painted as a background or hairline) and an ink
// (--X-ink, printed as small status text app-wide). They are separate tokens
// because on a light surface they pull in opposite directions: the fill has to
// stay vivid to read as the brand, while the ink has to go deep to stay legible.
// Both sides are guarded, but against different backdrops — an ink against the
// neutral surfaces it sits on, a fill against the --X-foreground label printed
// on it. Light is the fragile side: the dark palette's vivid accents read at
// 2.2-4.2:1 as text on a light surface, which is what this guard exists to catch.
const AA_SMALL_TEXT = 4.5;
const AA_NON_TEXT = 3;

function parseColor(value) {
  const match = /^#([\da-f]{3}|[\da-f]{6})$/i.exec((value ?? '').trim());
  if (!match) {
    return null;
  }

  const hex = match[1].length === 3 ? [...match[1]].map((channel) => channel + channel).join('') : match[1];
  return [0, 2, 4].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16));
}

// A ratio only means something between two opaque colours. Anything else -- an
// alpha channel, a var() indirection, a gradient, oklch() or color-mix() -- has
// no single value to measure, so the guard must say so instead of skipping the
// assertion it advertises.
function requireMeasurableColor(name, prop, value) {
  const rgb = parseColor(value);
  if (!rgb) {
    throw new Error(
      `${name}: ${prop} is ${value ?? 'undefined'}, which carries no measurable contrast. `
      + 'Guarded tokens must resolve to an opaque #rgb or #rrggbb value; a translucent or computed '
      + 'colour depends on its backdrop, so AA cannot be asserted from the token alone. Give the '
      + 'token an opaque value, or drop it from the guarded list and record why.',
    );
  }

  return rgb;
}

// A translucent token has no value of its own, but it does have one once you name
// the backdrop -- which is the whole reason the ring regressed unnoticed. Parsed
// separately from requireMeasurableColor so the strict rule stays strict: this
// path is only for tokens the guard deliberately composites against a stated
// surface, never a fallback for one that failed the opaque check.
function requireCompositableColor(name, prop, value) {
  const raw = (value ?? '').trim();
  const rgb = parseColor(raw);
  if (rgb) {
    return { rgb, alpha: 1 };
  }

  const match = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/i.exec(raw);
  if (!match) {
    throw new Error(
      `${name}: ${prop} is ${value ?? 'undefined'}, which the guard cannot composite. `
      + 'Composited tokens must be an opaque hex or an rgb()/rgba() with numeric channels.',
    );
  }

  return {
    rgb: [match[1], match[2], match[3]].map((channel) => Number.parseInt(channel, 10)),
    alpha: match[4] === undefined ? 1 : Number.parseFloat(match[4]),
  };
}

function compositeOver({ rgb, alpha }, backdrop) {
  return rgb.map((channel, index) => Math.round(alpha * channel + (1 - alpha) * backdrop[index]));
}

function relativeLuminance(rgb) {
  const [r, g, b] = rgb.map((channel) => {
    const ratio = channel / 255;
    return ratio <= 0.03928 ? ratio / 12.92 : ((ratio + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrastRatio(foreground, background) {
  const [lighter, darker] = [relativeLuminance(foreground), relativeLuminance(background)]
    .sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

// Measurements the owner has accepted below the WCAG floor. Avibe's accents carry
// the product's personality — an AI-colleague product should read as alive, not
// muted — so every accent keeps design.pen's value, and a brand value is never
// moved to buy contrast. What that costs is recorded here rather than hidden:
// the white label the design drew on a vivid light fill, and the mint focus ring
// at 50% over the light surfaces. Owner decision 2026-08-14.
//
// These are pinned, not skipped. Each entry records the ratio it was accepted at and
// the guard fails when the measured ratio moves off it, so an exemption still catches
// drift: nudge either token and this list goes stale loudly, forcing a fresh decision
// instead of quietly sliding further down.
const ACCEPTED_BRAND_RATIOS = new Map([
  ['light fill: --primary-foreground on --primary', 2.54],
  ['light fill: --accent-foreground on --accent', 3.68],
  ['light fill: --gold-foreground on --gold', 3.19],
  // Dark violet is not a new exemption: the Button already printed a hard-coded
  // text-white on this fill, so the pair shipped at 4.38:1 while no token declared
  // it and this guard could not see it. Naming --violet-foreground brings it under
  // the same pin as the rest instead of leaving it undeclared.
  ['dark fill: --violet-foreground on --violet', 4.38],
  // The light ring is design.pen's mint at 50%, which composites to ~1.5:1 — a hint
  // drawn beside a focused control, not the only cue for it. Dark needs no entry: the
  // same token over the dark surfaces clears the non-text floor on its own.
  ['light ring: --ring over --card', 1.61],
  ['light ring: --ring over --background', 1.55],
  ['light ring: --ring over --surface-3', 1.53],
]);
const PIN_TOLERANCE = 0.01;
const consultedBrandRatios = new Set();

// One ledger and one pin rule for every accepted measurement, whatever floor it was
// measured against: text and non-text differ only in the floor passed in, so a second
// exemption path can never drift away from this one.
function assertRatio(name, measured, ratio, floor) {
  const key = `${name}: ${measured}`;
  const accepted = ACCEPTED_BRAND_RATIOS.get(key);
  if (accepted !== undefined) {
    consultedBrandRatios.add(key);
    if (Math.abs(ratio - accepted) > PIN_TOLERANCE) {
      throw new Error(
        `${key} is ${ratio.toFixed(2)}:1, but ACCEPTED_BRAND_RATIOS pins it at ${accepted.toFixed(2)}:1. ` +
          'This measurement is exempt from the WCAG floor by an owner decision taken at that exact ratio, ' +
          'so moving either token needs a fresh decision — re-pin it here once the new value is intended.',
      );
    }
    return;
  }
  if (ratio < floor) {
    throw new Error(`${key} is ${ratio.toFixed(2)}:1, below WCAG AA ${floor}:1`);
  }
}

function assertContrast(name, tokens, inkProp, surfaceProp) {
  const ink = requireMeasurableColor(name, inkProp, tokens.get(inkProp));
  const surface = requireMeasurableColor(name, surfaceProp, tokens.get(surfaceProp));

  assertRatio(name, `${inkProp} on ${surfaceProp}`, contrastRatio(ink, surface), AA_SMALL_TEXT);
}

// A measurement that no longer exists must not keep its exemption: the tokens it
// excused may have been renamed or dropped, and a stale entry would silently excuse
// whatever takes that name next.
function assertEveryAcceptedRatioStillExists() {
  const stale = [...ACCEPTED_BRAND_RATIOS.keys()].filter((key) => !consultedBrandRatios.has(key));
  if (stale.length > 0) {
    throw new Error(
      `ACCEPTED_BRAND_RATIOS lists ${stale.join(', ')}, which no theme declares. Drop the entry.`,
    );
  }
}

// Every accent painted as a fill also gets used as text somewhere, so each one
// owes the palette an ink. Listing the fills (rather than deriving the pairs from
// whatever inks happen to exist) is what makes a new accent shipped without an
// ink a failure instead of a silent gap.
const ACCENT_FILLS = ['--primary', '--accent', '--destructive', '--mint', '--cyan', '--violet', '--gold', '--pink'];
const NEUTRAL_INKS = ['--foreground', '--muted'];
const INK_SURFACES = ['--card', '--background', '--surface-3'];

// A semantic fill and the palette token behind it must hold the same value, because
// the pair measured here is not always the pair rendered: Button's `brand` prints
// --primary-foreground on bg-mint and `brand-cyan` prints --accent-foreground on
// bg-cyan. They match today by convention only, so without this the pinned ratio on
// --primary would keep passing while the CTA it claims to describe drifted away.
const SEMANTIC_FILL_ALIASES = [['--primary', '--mint'], ['--accent', '--cyan']];

// Pointing at a control must never cost contrast. Every interactive brand fill
// therefore declares the value it hovers to, and the hovered fill is measured against
// the same label as the resting one. Naming the hovered value is the point: a filter
// scales the label along with the background, so it cannot be aimed — brightness(1.1)
// on light --primary read 2.09:1 against the 2.54:1 it started from, and no brightness
// value fixes it, because a white label is already clamped at the top.
//
// The direction follows the LABEL, not the theme. Mint, cyan and gold carry a dark
// label in dark and a white one in light, so they lighten there and darken here;
// --violet's label is white in both, so it darkens in both. That inversion is exactly
// what a single shared mix token got wrong, and what this assertion catches.
// --mint / --cyan ride along through SEMANTIC_FILL_ALIASES above.
const HOVER_FILLS = ['--primary', '--accent', '--gold', '--violet', '--destructive'];

for (const [theme, tokens] of [['light', systemLight], ['dark', systemDark]]) {
  for (const fill of ACCENT_FILLS) {
    assertEqual(`${theme} ${fill} is defined`, tokens.has(fill), true);
    assertEqual(`${theme} ${fill} declares an ink`, tokens.has(`${fill}-ink`), true);
    // --X-ink names the role an accent plays (a mark or small text rather than a
    // fill); it is not a second, deepened palette. Both roles carry design.pen's
    // value, so the ink equals the fill, and a light accent therefore reads
    // 2.1-4.2:1 as text — under the AA floor by the owner decision above. This
    // identity is what keeps that decision honest: re-deepening an ink to buy
    // contrast fails here instead of shipping a palette nobody drew.
    if (tokens.get(`${fill}-ink`) !== tokens.get(fill)) {
      throw new Error(
        `${theme} ${fill}-ink is ${tokens.get(`${fill}-ink`)} but ${fill} is ${tokens.get(fill)}. `
          + 'An accent keeps its design value in both roles; move the design value if it should '
          + 'change, and move both.',
      );
    }
  }

  for (const [semantic, palette] of SEMANTIC_FILL_ALIASES) {
    // The hover pair is aliased for the same reason as the resting one: `brand` pairs
    // bg-mint with --primary-foreground, so a --mint-hover that drifted from
    // --primary-hover would render a pairing no assertion below ever measures.
    for (const suffix of ['', '-hover']) {
      const [a, b] = [`${semantic}${suffix}`, `${palette}${suffix}`];
      if (tokens.get(a) !== tokens.get(b)) {
        throw new Error(
          `${theme} ${a} is ${tokens.get(a)} but ${b} is ${tokens.get(b)}. `
            + `Button prints ${semantic}-foreground on ${palette}, so the contrast measured on ${a} `
            + 'is only the contrast users see while the two stay equal. Move both or neither.',
        );
      }
    }
  }

  for (const fill of HOVER_FILLS) {
    assertEqual(`${theme} ${fill} declares a hover fill`, tokens.has(`${fill}-hover`), true);
    const name = `${theme} hover`;
    const label = requireMeasurableColor(name, `${fill}-foreground`, tokens.get(`${fill}-foreground`));
    const resting = contrastRatio(label, requireMeasurableColor(name, fill, tokens.get(fill)));
    const hovered = contrastRatio(label, requireMeasurableColor(name, `${fill}-hover`, tokens.get(`${fill}-hover`)));
    if (hovered < resting) {
      throw new Error(
        `${theme} ${fill}-hover reads ${hovered.toFixed(2)}:1 against ${fill}-foreground, worse than `
          + `${fill}'s own ${resting.toFixed(2)}:1. Hovering a control must not cost contrast, so the `
          + `hovered fill has to move AWAY from its label — darker under a light label, lighter under a `
          + 'dark one. Check which way this theme\'s label points before picking the value.',
      );
    }
  }

  // Inks are read as small text on a bare surface, so every one of them clears AA
  // against the darkest surface the theme owns. Derived from the token map rather
  // than from ACCENT_FILLS so an ink added on its own is still covered. Accent inks
  // are the exception and are covered by the identity assertion above instead: they
  // are brand values, and a floor here would just re-deepen the palette. Every ink
  // therefore lands under exactly one of the two rules — none under neither.
  const accentInks = new Set(ACCENT_FILLS.map((fill) => `${fill}-ink`));
  const inks = [
    ...NEUTRAL_INKS,
    ...[...tokens.keys()].filter((prop) => prop.endsWith('-ink') && !accentInks.has(prop)),
  ];
  for (const ink of inks) {
    assertEqual(`${theme} ${ink} is defined`, tokens.has(ink), true);
    for (const surface of INK_SURFACES) {
      assertContrast(`${theme} ink`, tokens, ink, surface);
    }
  }

  // Fills are read through the label printed on them, so every fill that declares
  // its own foreground is checked against it. A fill can then stay as vivid as the
  // design asks without ever silently stranding that label.
  const pairs = [...tokens.keys()]
    .filter((prop) => prop.endsWith('-foreground') && tokens.has(prop.slice(0, -'-foreground'.length)))
    .map((prop) => [prop, prop.slice(0, -'-foreground'.length)]);
  assertEqual(`${theme} fill pairs exist`, pairs.length > 0, true);
  for (const [foreground, fill] of pairs) {
    assertContrast(`${theme} fill`, tokens, foreground, fill);
  }

  // The focus ring is the one keyboard-only affordance in the app, and it is
  // translucent by design, so it is measured composited against the surfaces it
  // is drawn over rather than as a token. Non-text, hence the 3:1 floor.
  const ring = requireCompositableColor(`${theme} ring`, '--ring', tokens.get('--ring'));
  for (const surfaceProp of INK_SURFACES) {
    const surface = requireMeasurableColor(`${theme} ring`, surfaceProp, tokens.get(surfaceProp));
    const ratio = contrastRatio(compositeOver(ring, surface), surface);
    assertRatio(`${theme} ring`, `--ring over ${surfaceProp}`, ratio, AA_NON_TEXT);
  }
}

// A Tailwind utility is named after the @theme alias, not after the token behind
// it, so an alias is free to expose an accent under a name that hides which side
// of the fill/ink split it lands on. That is exactly how `text-success` survived
// the split: it resolved through `--color-success: var(--mint)` to the vivid
// fill, landing at 2.5:1 as text, while the ink guard above saw nothing wrong --
// `--mint-ink` was correct, nothing simply pointed at it. So an alias onto an
// accent token must carry that token's own name, which puts every accent utility
// back under the ink and fill assertions.
function assertAccentAliasesKeepTheirTokenName(source) {
  const accentTokens = new Set(ACCENT_FILLS.flatMap((fill) => [
    fill,
    `${fill}-ink`,
    `${fill}-soft`,
    `${fill}-foreground`,
    `${fill}-hover`,
  ]));

  postcss.parse(source).walkAtRules('theme', (atRule) => {
    atRule.walkDecls((decl) => {
      const target = /^var\(\s*(--[\w-]+)\s*\)$/.exec(normalizeCssValue(decl.value))?.[1];
      if (!target || !accentTokens.has(target)) {
        return;
      }

      const expected = `--color-${target.slice(2)}`;
      if (decl.prop !== expected) {
        throw new Error(
          `@theme alias ${decl.prop}: var(${target}) renames an accent token. It ships a utility whose `
          + `name hides whether it paints the fill or the ink, so it escapes both contrast assertions. `
          + `Name it ${expected} and let each call site pick the fill or the ink explicitly.`,
        );
      }
    });
  });
}

assertAccentAliasesKeepTheirTokenName(css);

// What a glow is, and why a literal one cannot be allowed, is stated in
// `shadowLayer.mjs` -- along with the classifier that answers it. That rule is
// the one this file exists to enforce; what stays here is everything about
// WHERE a value can come from, which is the other half of the gate.

// A glow layer has exactly one legal spelling: a reference to a --shadow-glow-*
// token, as the whole layer. Not "a managed colour with any geometry" -- that
// was this test's third hole in as many rounds, and the three share one shape.
// Each time the predicate checked something narrower than the error message
// claimed: first the colour, by listing two literal spellings out of the many
// CSS accepts; then the input, by reading one of the four channels a shadow
// value arrives through; then the geometry, by passing `0 0 93px var(--mint)`
// because every part was individually well-formed while the shape it draws was
// invented on the spot. Checking the parts one axis at a time is what keeps
// leaving an axis uncovered, so they are not checked one axis at a time any
// more. A glow names a token; a layer that spells out any of its own offsets,
// blur, spread or colour is an offender however each piece is written. That
// leaves no fourth axis to hide behind and makes the predicate say precisely
// what the message below says.
//
// Lengths are still recognised, but only to tell a hand-written glow apart from
// a token reference so it can be rejected -- never to bless one part of it. The
// grammar itself lives in cssLength.mjs, with its own tests: it is a rule about
// CSS rather than about this scan, and the enumeration it replaces was the
// fourth time this file checked something narrower than its message claimed.

// Every channel a shadow value reaches the page through. The first cut of this
// scan read only `shadow-[...]`, which repeated one level up the mistake the
// colour test above was written to stop: it enumerated its own input, so a
// `[box-shadow:...]` written the next day would have walked straight past the
// assertion meant to catch it. The fix then claimed this list "can genuinely be
// closed -- a shadow is a Tailwind arbitrary value, a Tailwind arbitrary
// property, a CSS declaration or an inline style object, and the stack offers no
// fifth way to write one", and pointed at SHADOW_MENTION as the measurement that
// kept the claim honest.
//
// `filter: drop-shadow(0 0 4px …)` was the fifth way, and it did not merely slip
// the channels -- it slipped the measurement too, silently, because the mention
// regex was written by listing the same spellings the channels already read. A
// completeness check assembled from the list it is policing cannot discover that
// the list is short; it can only confirm the entries it was handed. That is why
// three hand-drawn wire glows sat in modelHubSurface.css through five rounds of
// this guard reporting all-clear.
//
// So the claim is retired rather than re-made: this list is NOT closed by
// argument, and no amount of thinking about the stack will close it. It is held
// closed from the outside by SHADOW_MENTION, which now derives from the word
// itself instead of from these entries, so the next spelling nobody here has
// imagined arrives as a loud failure the day it lands.

// The one shape where the property is NAMED rather than written: CSSOM's
// `setProperty(name, value)` passes `box-shadow` as a string argument, so the
// word is followed by the quote that closes it and then a comma -- never by the
// `:`, `=` or opening delimiter that every other spelling puts there. Both the
// channel and SHADOW_MENTION key off that introducing punctuation, so this went
// unread AND uncounted: the exact double silence they exist to prevent.
//
// The narrow anchor on `.setProperty(` is deliberate and is not the enumeration
// that cost the earlier rounds. Those were open sets -- property names, quote
// styles, delimiters -- where the next member was always someone's next idea.
// This one is closed by the DOM: `setProperty` is the only by-name setter on
// CSSStyleDeclaration, and `style.boxShadow =` / `style['boxShadow'] =` are
// assignments the channel above already reads. Widening it to "any quoted
// property name followed by a comma" was tried and is wrong in the other
// direction: `cn('shadow-mint-card', …)` is a class list, not an assignment,
// and three of those already ship.
//
// `\x60` is a backtick -- a template-literal property name is exotic, but
// "exotic" is the argument that lost the last six rounds.
// The `(?!--)` is the same judgement `SHADOW_MENTION` already made on the CSS
// side, brought to the branch that had not heard it: `--shadow-color` is a
// custom property, not a shadow property, and `setProperty('--shadow-color',
// '#fff')` therefore draws nothing. Reading its value as a whole box-shadow
// failed `validate:theme` on a colour -- a false positive, in CI, on a file that
// was correct.
//
// Excluding the NAME was the wrong repair, and the argument for it was wrong in
// a way worth keeping written down: "a custom property is inert, it renders only
// where some declaration spends it, and that declaration IS scanned" holds for
// the value a stylesheet declares and not for the one assigned at runtime.
// `--x: none` with `box-shadow: var(--x)` is scanned, innocent, and complete --
// and then `setProperty('--x', '0 0 93px red')` supplies the glow from outside
// the stylesheet, past a scan that already read every declaration and found
// nothing. The static half is not where the value comes from.
//
// So the name is read again and the exemption moves to the VALUE, which is where
// the original false positive actually lived: `setProperty('--shadow-color',
// '#fff')` is innocent because a colour carries no geometry, not because a
// custom property cannot draw. That is the same judgement `shadow-(color:--x)`
// already makes one channel up, made with the same COLOUR recogniser, so it adds
// no new notion of what a glow is -- which was the real objection to matching on
// "values that look like glow geometry", and it is not what this does: anything
// that is not provably a colour stays readable and gets classified.
const CSSOM_SETTER = /\.setProperty\(\s*(?<quote>['"\x60])(?<property>[^'"\x60]*shadow[^'"\x60]*)\k<quote>\s*,/;

// What counts as a shadow PROPERTY, written once. `box-shadow`, `text-shadow`
// and `drop-shadow` all end in the word; `shadowPreset` and `shadowRoot` only
// start with it and are not properties at all, and a `--*` declaration is a
// custom property rather than a shadow one.
//
// This used to be spelled twice: the CSS declaration channel read `box-shadow`
// and only it, while the completeness matcher read any `*shadow`. The comment on
// SHADOW_MENTION already said "the two must narrow together" -- but saying so is
// not a mechanism, and they diverged anyway, in the direction that fails correct
// files: `text-shadow: var(--shadow-glow-md-mint)` was counted as a mention, read
// by no channel, and reported as an unscanned channel, so a fully tokenized
// declaration blocked validate:theme. Sharing the source is that discipline in
// the form that cannot be forgotten, which is what CSSOM_SETTER is already doing
// one definition down.
const SHADOW_PROPERTY = `(?<![\\w-])(?!--)[A-Za-z-]*shadow(?![A-Za-z])`;

// Sharing the pattern closed the divergence by NAME and left it open by FLAG:
// the CSS channels ran case-sensitively while SHADOW_MENTION ran `gi`, so
// `BOX-SHADOW: var(--shadow-glow-sm-mint)` -- a valid declaration, CSS property
// names being ASCII case-insensitive -- was measured as a mention, read by no
// channel, and reported as an unscanned channel. The same failure as the
// `text-shadow` one, arriving through the argument that was not shared.
//
// Case-insensitivity alone is not safe here, and the reason is worth keeping:
// this scan runs one set of channels over three languages, so `i` also lets the
// CSS channels match `boxShadow:` in a `.tsx` file -- where the value is a JS
// expression, and reading it with CSS's terminators stops at the first quote.
// `boxShadow: active ? `…` : undefined` then yields `active ?` and the guard
// fails the tree it is guarding. That is not hypothetical; it is what this
// change did on its first draft.
//
// The hyphen is what separates the two languages. Every CSS shadow property has
// one -- `box-shadow`, `text-shadow`, `-webkit-box-shadow` -- and no JS style
// key does, because camelCase is the whole point of the JS spelling. Requiring
// it here is what makes `i` safe, and it removes an overlap that was previously
// avoided only by the accident of `boxShadow`'s capital S. Written as a
// lookahead over the shared definition, so the rule about where the word sits
// still has exactly one home.
const CSS_SHADOW_PROPERTY = `(?=[A-Za-z-]*-shadow)${SHADOW_PROPERTY}`;
const PROPERTY_FLAGS = 'gi';

const cssomArgument = (match) => valueArgument(match.input, match.index + match[0].length) ?? '';

// The expression a style property is given, read from where the match ends. An
// empty string when it never terminates, which yields no values and excuses
// nothing, so the site is reported as unreadable rather than passed over.
const styleExpression = (match, tree) =>
  propertyExpression(match.input, match.index + match[0].length, tree) ?? '';

// A custom property assigned a bare colour contributes no shadow value: it tints
// geometry that some scanned declaration still has to supply. Both halves of the
// channel below ask through this one function, because "what this match yields"
// and "why yielding nothing is innocent rather than unreadable" are the same
// judgement, and the two spellings of it are exactly what drifts apart.
//
// Only `--*` earns it. `setProperty('box-shadow', '#fff')` names the shadow
// property itself, and a colour there is a value to read, not one to excuse.
const isTint = (match) => {
  if (!match.groups?.property?.startsWith('--')) return false;
  const values = stringLiterals(cssomArgument(match));
  return values.length > 0 && values.every((value) => COLOUR.test(value.trim()));
};

const SHADOW_CHANNELS = [
  // `shadow-[0_0_16px_-4px_var(--x)]`, including variants such as `hover:shadow-[…]`.
  { pattern: /shadow-\[([^\]]*)\]/g, valuesOf: (match) => [match[1]] },
  // `[box-shadow:0_0_16px_-4px_var(--x)]` -- Tailwind's arbitrary *property*.
  {
    pattern: new RegExp(`\\[${CSS_SHADOW_PROPERTY}\\s*:([^\\]]*)\\]`, PROPERTY_FLAGS),
    valuesOf: (match) => [match[1]],
  },
  // `shadow-(--x)` and `drop-shadow-(--x)` -- Tailwind's custom-property
  // shorthand, which compiles to `box-shadow: var(--x)` and to the same inside
  // `drop-shadow()`. Parentheses instead of brackets is the whole difference
  // from the arbitrary-value form above, and it is enough to park a glow in:
  // `--rogue-glow: 0 0 93px red` rendered through `shadow-(--rogue-glow)` was
  // read by no channel. The name is handed on as a `var()` so it resolves
  // through exactly the same indirection as every other reference -- this
  // channel translates a spelling, it does not get its own idea of what a glow
  // is.
  {
    pattern: /(?<![\w-])(?:drop-)?shadow-\(([^)]*)\)/g,
    valuesOf: (match) => [CUSTOM_PROPERTY.test(match[1].trim()) ? `var(${match[1].trim()})` : ''],
    // `shadow-(color:--x)` sets the shadow COLOUR and carries no geometry, so it
    // cannot draw a glow by itself; the geometry it tints still has to come from
    // a utility this scan reads. Anything else inside the parentheses is a form
    // this scan cannot follow, and stays unreadable rather than accepted.
    provablyNotAShadow: (match) => match[1].trim().startsWith('color:'),
  },
  // `box-shadow: 0 0 16px -4px var(--x);` in a stylesheet. The lookbehind hands
  // the arbitrary-property spelling to the channel above rather than matching it
  // twice, once with a stray `]` glued to the colour. A declaration also ends at
  // a quote, because this same spelling appears inside assertion strings in the
  // `.ts` files scanned alongside the CSS -- and a shadow value of its own never
  // contains one, so the quotes cost nothing to stop at and stop the closing one
  // from being read as part of the colour.
  //
  // A property name followed by a colon is the SPELLING of a declaration, and
  // this channel used to treat it as one wherever it appeared. Two things are
  // spelled that way and declare nothing: a pseudo-class on a class named after
  // a property -- `.box-shadow:hover { color: red }`, whose `hover { color: red`
  // went to the shadow classifier and failed valid CSS -- and any sentence in a
  // `.ts` file, where `const example = 'box-shadow: 0 0 93px red'` is text about
  // CSS rather than CSS. Both are answered by asking where the match IS instead
  // of what it looks like, and both parsers already know: `cssDeclarations.mjs`
  // says which offsets a stylesheet declares at, `nonRenderingText.mjs` says
  // which stretches of a TypeScript file are a stylesheet at all.
  //
  // The match is still claimed, and refused with a reason rather than dropped.
  // Claiming keeps the completeness check honest -- the mention matcher counts
  // this spelling, so a channel that stopped matching it would turn a false
  // positive into a different failure with a stranger message -- and refusing it
  // through `provablyNotAShadow` says what is actually true: not that the value
  // is innocent, but that there is no declaration here to have a value.
  {
    pattern: new RegExp(`(?<!\\[)${CSS_SHADOW_PROPERTY}\\s*:([^;}'"\`]*)`, PROPERTY_FLAGS),
    valuesOf: (match, tree, declares) => (declares(match.index) ? [match[1]] : []),
    provablyNotAShadow: (match, tree, declares) => !declares(match.index),
  },
  // `filter: drop-shadow(0 0 4px …)`. Matched at the FUNCTION rather than at the
  // property, because the property is not the thing there is only one of: a drop
  // shadow can arrive through `filter`, `-webkit-filter`, `backdrop-filter`, a
  // Tailwind arbitrary property or an inline style object, and reading it at
  // `drop-shadow(` covers all of them at once instead of collecting five more
  // entries here that would then need a sixth. The argument list is taken by
  // balancing parentheses because a shadow colour is routinely a `color-mix(…)`
  // and a `[^)]*` would stop inside it.
  {
    pattern: /drop-shadow\(/gi,
    valuesOf: (match) => [balancedArgument(match.input, match.index + match[0].length - 1)],
  },
  // `style={{ boxShadow: … }}` and `el.style.boxShadow = …`. What follows is an
  // expression rather than a value, so read the string literals out of it: a
  // ternary's two branches are two shadows and both of them render. Any
  // camelCase identifier carrying `shadow` is read, not `boxShadow` alone --
  // `textShadow` and `dropShadow` are the same channel, and enumerating them one
  // by one is how this list fell short the first time. The hyphen in the
  // lookbehind keeps CSS's `box-shadow` with the declaration channel above.
  {
    // The optional quote and bracket carry the same three spellings
    // SHADOW_MENTION accepts -- `boxShadow:`, `'boxShadow':`, `['boxShadow'] =`
    // -- so the channel reads exactly what the measurement counts. Widening one
    // without the other is how a spelling becomes scanned-but-unreported or
    // reported-but-unscannable, and both of those are silence.
    //
    // The identifier ENDS at the word -- and, for an assignment, the target is
    // `element.style`. Both halves are the same finding arriving twice:
    // `[Ss]hadow[A-Za-z]*` matched any name merely containing the word, so
    // `const shadowPreset = 'compact'` was read as a style property; ending at
    // the word fixed that spelling and left `const cardShadow = 'compact'`
    // reading as one, because a name cannot answer the question. STYLE_ASSIGNMENT
    // answers it where the answer lives, at the assignment target, and is shared
    // with SHADOW_MENTION so the two cannot drift apart.
    pattern: new RegExp(SHADOW_KEY, 'g'),
    valuesOf: (match, tree) => (isStyleWrite(match.index, tree)
      ? stringLiterals(styleExpression(match, tree))
      : []),
    // Two ways to be provably not a shadow, and they answer different questions.
    //
    // `isStyleWrite` asks whether this is a style write AT ALL. The pattern
    // finds a key by the punctuation after it, and a colon in TypeScript also
    // makes a type member and a destructuring binding -- `interface Props {
    // boxShadow: string }` and `const { boxShadow: current } = style` were both
    // claimed as rendered assignments and then failed as unreadable, for two
    // constructs that never reach a browser.
    //
    // NON_STRING_LITERAL asks, of a real style write, whether its value can be
    // a CSS shadow: a CSS shadow is a string, so `scrollbar: { useShadows:
    // false }` is a Monaco flag. That one is deliberately narrower than "no
    // string literal here" -- `boxShadow: glow` is an identifier that could hold
    // anything, and stays unreadable rather than becoming innocent.
    provablyNotAShadow: (match, tree) => !isStyleWrite(match.index, tree)
      || NON_STRING_LITERAL.test(styleExpression(match, tree)),
  },
  // `el.style.setProperty('box-shadow', '0 0 93px red')`. Read through the
  // shared CSSOM_SETTER source rather than a pattern of its own, because the
  // measurement below has to recognise the identical span: these two have been
  // widened in lockstep by hand for three rounds, and a shared source is the
  // version of that discipline which cannot be forgotten.
  {
    pattern: new RegExp(CSSOM_SETTER.source, 'gi'),
    valuesOf: (match) => (isTint(match) ? [] : stringLiterals(cssomArgument(match))),
    provablyNotAShadow: (match) => isTint(match) || NON_STRING_LITERAL.test(cssomArgument(match)),
  },
];

// The string literals in an expression: a ternary's two branches are two
// shadows and both of them render.
function stringLiterals(expression) {
  if (expression === null) return [];
  return [...expression.matchAll(/'([^']*)'|"([^"]*)"|`([^`]*)`/g)]
    .map(([, single, double, template]) => single ?? double ?? template);
}


// The parenthesis-balanced span starting at `openIndex`, or null when it never
// closes -- null is not a quiet skip, it lands in the unreadable bucket.
function balancedArgument(source, openIndex) {
  let depth = 0;
  for (let index = openIndex; index < source.length; index += 1) {
    if (source[index] === '(') depth += 1;
    else if (source[index] === ')' && (depth -= 1) === 0) return source.slice(openIndex + 1, index);
  }
  return null;
}

// The whole expression is one non-string literal. Anchored on where the literal
// ENDS rather than on the punctuation trailing it: written as "the rest of the
// capture is closing syntax" it turned on how the surrounding JSX happened to be
// spelled, and it has to close the value instead, so that `false || glow` -- a
// literal followed by something that could hold anything -- stays unreadable.
const CUSTOM_PROPERTY = /^--[A-Za-z0-9_-]+$/;
const NON_STRING_LITERAL =/^\s*(true|false|null|undefined|[+-]?\d+(\.\d+)?)\s*($|[,;}\])])/;

// A glow is a *value*, and there are only so many ways to introduce one: a `:`
// in a declaration or an object literal, an `=` in an assignment, a `(` opening
// a filter function, and Tailwind's `shadow-[`. Naming the property without
// carrying a value cannot hide a glow -- `transition-[background-color,
// box-shadow]`, `will-change: box-shadow`, a sentence in a comment -- so those
// are not mentions. What the measurement pins is exactly what failed before:
// every place a shadow value is INTRODUCED is also a place it is READ.
//
// The name is matched as ANY identifier containing the word, not as the three
// spellings the channels happen to read. That difference is the entire value of
// this line. Written the old way it listed `shadow-[`, `box-shadow:` and
// `boxShadow`, which is to say it asked the channels what to look for and then
// confirmed they were looking for it -- so `filter: drop-shadow(…)` was not
// reported as an unscanned spelling, it was not reported at all, and the guard
// that exists to make a missing channel loud stayed silent about the one channel
// it was missing. Derived from the word instead, the check can fail in the
// direction that matters: it does not need to know what a shadow may be called
// tomorrow to notice that something called one went unread.
//
// Custom-property DECLARATIONS are excluded, and only they. `--x-shadow: 0 0 …`
// is the token layer -- the one sanctioned home for a shadow literal -- and it
// is not skipped so much as reached from the other end: call sites resolve their
// names into it, so a glow parked in a token is caught when something uses it,
// and a token nothing uses draws no light on any page.
//
// The punctuation between the name and its `:`/`=` is optional because JS lets
// a property be spelled three ways that mean one thing: `boxShadow:`,
// `'boxShadow':` and `['boxShadow'] =`. Requiring the colon to touch the name
// read the quote as the end of the mention, so `el.style['boxShadow'] = '0 0
// 93px red'` was not scanned AND not reported -- the exact silent gap this line
// exists to close, reopened by the punctuation rather than by the word.
//
// What follows the word is an OPENING DELIMITER or an ASSIGNMENT, which is the
// grammar of "this name is about to be given a value" -- not a list of the three
// spellings that grammar happened to take. Written as a list it read `(`, `-[`
// and `:`/`=`, and Tailwind's `shadow-(--x)` shorthand fell between them: the
// `-(` is neither of the first two, so a glow parked in `--rogue-glow` and used
// as `shadow-(--rogue-glow)` was, once again, not scanned and not reported. The
// enumeration had moved out of the word and into the punctuation after it.
//
// The second branch is CSSOM_SETTER's own source, not a copy of it. The first
// branch says "the word, then punctuation that hands it a value", which is the
// grammar of every spelling written as syntax; `setProperty('box-shadow', …)`
// names the property instead of writing it, so it has no such punctuation and
// no restatement of the first branch can reach it. Sharing the source is what
// keeps the channel and this measurement describing one span: widen one alone
// and a spelling becomes either scanned-but-uncounted or counted-but-unscannable,
// and both of those are silence.
//
// A colon means two different things in the two languages this scan reads, and
// measuring both with one rule is what made `{ cardShadow: 'compact' }` a
// finding. In CSS a colon makes a DECLARATION, so the property before it is
// whatever the spec says it is -- the word rule has to stay open there, or the
// next shadow property CSS grows goes uncounted. In JavaScript a colon makes an
// OBJECT KEY, and an object key only paints when it names a real CSS property:
// `cardShadow` is a variable, `style.cardShadow = x` draws nothing, and there is
// no future spelling of it that will. So the CSS half keeps the open word rule
// and the JS half takes the closed list, which is what SHADOW_KEY already is.
//
// The JS half is not merely read from the same list as the channel, it IS the
// channel's pattern. That is the one narrowing the error message below warns
// against making by hand -- "do NOT narrow SHADOW_MENTION to make this pass" --
// and sharing the pattern is what makes it safe rather than forbidden: a mention
// counted here and claimed by no channel is a loud failure, a mention claimed by
// a channel and not counted here is a silent one, and the two cannot land on
// opposite sides of that line while they are the same string. Three rounds of
// widening these in lockstep by hand ended in a drift anyway; a copy that has to
// be edited twice is a copy that will be edited once.
// Two patterns rather than one, because case sensitivity is a property of the
// LANGUAGE and not of this matcher. CSS folds the case of a property name --
// `BOX-SHADOW: red` renders -- so the CSS half must be case-insensitive or it
// misses. JavaScript does not fold anything: `boxShadow` and `BOXSHADOW` are two
// different keys, and only one of them is a CSS property.
//
// One `i` flag over both halves therefore claimed to measure what the channel
// checks and did not, in the one direction that costs a pull request. The JS
// channel matches case-sensitively, as it must; the mention matcher counted
// `const o = { BOXSHADOW: 'compact' }` and `el.style.boxshadow = x` all the same
// -- names that address no CSS property and draw nothing -- and reported them as
// spellings no channel reads. It also hid the finding underneath: the real
// CSSOM spelling `webkitBoxShadow` was missing from the property list, and the
// `i` flag had been papering over its absence by matching the capitalised alias
// case-blind.
//
// So the JS half is the channel's own pattern, flags included, which is what
// makes the promise above -- that a mention and a channel cannot land on
// opposite sides of the line -- true rather than nearly true.
const SHADOW_MENTIONS = [
  new RegExp(
    `${CSS_SHADOW_PROPERTY}\\s*:`
    + `|${SHADOW_PROPERTY}-?[([]`
    + `|${CSSOM_SETTER.source}`,
    'gi',
  ),
  new RegExp(SHADOW_KEY, 'g'),
];

// Every custom property declared anywhere in the scanned stylesheets, name to
// the set of values it is given. A name is collected once per distinct value
// rather than last-write-wins, because a property is routinely declared several
// times -- dark, `prefers-color-scheme: light`, `[data-theme="light"]` -- and a
// glow smuggled into just one of those blocks is still a glow that ships.
// What counts as a declaration of a name lives in `customProperties.mjs`, where
// it can be called from a test; this is only the fold across stylesheets.
function collectCustomProperties(root) {
  const values = new Map();
  for (const relative of intendedFiles(root, { extensions: ['.css'] })) {
    customPropertiesIn(fs.readFileSync(path.join(root, relative), 'utf8'), values);
  }
  return values;
}

// The names a registration constrains to colours, which is the one thing that
// can prove a `var()` in a shadow's third slot holds no radius. Folded across
// stylesheets by the same shape as the values above, and answered in
// `customProperties.mjs` for the same reason: what a registration promises is a
// question about CSS grammar, and a question about grammar wants cases.
function collectColourRegistrations(root) {
  const names = new Set();
  for (const relative of intendedFiles(root, { extensions: ['.css'] })) {
    colourRegistrationsIn(fs.readFileSync(path.join(root, relative), 'utf8'), names);
  }
  return names;
}

// The VALUES declared inside an `@theme` block, per name -- the token layer
// itself, as opposed to everything that merely looks like it. Collected
// separately from `collectCustomProperties` because the two answer different
// questions: that one asks what a name is worth anywhere, this one asks which
// of those worths were sanctioned.
//
// Values rather than names, because a name is not a place either. Recording
// membership as a name made the sanction transferable: `--shadow-glow-wire-cyan`
// is declared in `@theme`, so the name is managed forever, and a component
// stylesheet redeclaring it as `0 0 93px red` inherited that trust -- the
// override was collected, marked managed and discarded unread, while the
// cascade handed the call site the override at runtime. That is the same
// name-for-place substitution the previous round made one level up, still
// standing one level down; asking which declarations are sanctioned rather than
// which names appear closes it, because an out-of-theme declaration is then a
// value the set does not contain and falls through to ordinary classification.
function collectThemeDeclarations(root) {
  const values = new Map();
  for (const relative of intendedFiles(root, { extensions: ['.css'] })) {
    postcss.parse(fs.readFileSync(path.join(root, relative), 'utf8')).walkAtRules('theme', (rule) => {
      rule.walkDecls((decl) => {
        if (!decl.prop.startsWith('--')) return;
        if (!values.has(decl.prop)) values.set(decl.prop, new Set());
        values.get(decl.prop).add(decl.value);
      });
    });
  }
  return values;
}


// Token definitions (`--shadow-glow-cta-mint: …`) name none of the channel
// spellings and so are never scanned as call sites, which is the point: the
// token layer is the one place a literal is allowed to live, because it is the
// one place light can re-anchor it. What this walks is call sites -- but it now
// follows their names into that layer, so defining a glow under some other name
// is not a way around it.
function assertGlowsReadThroughTokens(root) {
  const offenders = [];
  const unscanned = [];
  const unreadable = [];
  const tokens = {
    values: collectCustomProperties(root),
    managed: collectThemeDeclarations(root),
    colours: collectColourRegistrations(root),
  };

  for (const relative of intendedFiles(root, { extensions: ['.ts', '.tsx', '.css'] })) {
    // A test is not a page. Its strings document values rather than drawing
    // them, and Vite never bundles the file, so scanning it turns this gate into
    // one that fails a test for containing the string it is testing.
    if (!rendersAtAll(relative)) continue;

    const file = path.join(root, relative);
    const raw = fs.readFileSync(file, 'utf8');
    const source = withoutNonRenderingText(raw, file);

    // The tree of the file AS WRITTEN, which the JS channels need to say where a
    // value ends. `withoutNonRenderingText` has already parsed exactly this text,
    // so the memo makes it the same tree rather than a second parse. CSS has no
    // tree here and no channel that asks for one.
    const tree = file.endsWith('.css') ? null : parseSource(raw, file);

    // And where the file DECLARES, for the one channel whose spelling a selector
    // and a sentence can both wear. Two parsers answer it between them: which
    // stretches of this file are CSS at all, then which offsets inside those
    // stretches CSS declares at, folded back into the coordinates of the file
    // they came from.
    //
    // The file AS WRITTEN, like the tree above and for a sharper reason. Blanking
    // preserves offsets but not grammar -- a blanked `@media (…)` is an `@`
    // followed by spaces, which is not a stylesheet any parser will read -- so
    // asking the blanked text would fail every parse and, deny-by-default, call
    // the whole file a declaration. Nothing is lost by asking the real text:
    // blanking has already been applied to the matches themselves, so a span that
    // does not render produces no match to ask about.
    const declarations = cssRangesIn(raw, file).reduce(
      (spans, [start, end]) => declarationSpansIn(raw.slice(start, end), start, spans),
      [],
    );
    const declares = (index) => declaresAt(declarations, index);
    const claimed = [];

    for (const { pattern, valuesOf, provablyNotAShadow } of SHADOW_CHANNELS) {
      for (const match of source.matchAll(pattern)) {
        claimed.push([match.index, match.index + match[0].length]);

        // Matching a mention silences the completeness check below, so a channel
        // that matches and then yields nothing is the one way left to be scanned
        // and unchecked at the same time. `style={{ boxShadow: glow }}` is that
        // case: the expression holds no string literal, so there is no value to
        // test, and moving a literal into a constant would slip the gate. A
        // channel that claims a mention owes a value, and owing nothing fails.
        const values = valuesOf(match, tree, declares).filter((value) => value && value.trim());
        if (values.length === 0) {
          if (provablyNotAShadow?.(match, tree, declares)) continue;
          unreadable.push(`${file}:${source.slice(0, match.index).split('\n').length}: ${
            match[0].split('\n')[0].slice(0, 80)}`);
          continue;
        }

        // `drop-shadow()` takes no spread, and a layer carrying one is dropped
        // whole rather than drawn wrong, so the call site adds a constraint the
        // token layer cannot know about.
        const dropShadow = readsIntoDropShadow(source, match.index, match[0]);

        for (const value of values) {
          for (const offence of glowOffencesInValue(value, tokens)) {
            offenders.push(`${file}: ${offence}`);
          }
          if (!dropShadow) continue;
          for (const offence of spreadOffencesInDropShadow(value, tokens)) {
            offenders.push(`${file}: ${offence}`);
          }
        }
      }
    }

    // A mention is claimed when some channel READ THE SAME TEXT, which is an
    // overlap of two spans and not the containment of one offset in the other.
    //
    // The two matchers now share the property name, the flags, the style key and
    // the case rules -- four rounds, each closing one quantity they had spelled
    // twice. This is the fifth and last: WHERE the match begins. A channel is
    // free to claim less text than the mention points at, because a utility's
    // prefix carries no value -- `drop-shadow-[var(--shadow-glow-wire-mint)]` is
    // read for what is inside the brackets, so the claim starts at `shadow-[`
    // while the mention starts at `drop-`. Comparing start offsets called that
    // unscanned and failed a correct, fully tokenized utility; comparing spans
    // asks the question the measurement was always for.
    //
    // Overlap cannot loosen the check, because the mention list does not check
    // anything -- the channels do. Its only job is to make a channel's gaps
    // loud, and a channel that read this text has no gap here.
    for (const pattern of SHADOW_MENTIONS) {
      for (const mention of source.matchAll(pattern)) {
        const [from, to] = [mention.index, mention.index + mention[0].length];
        if (!claimed.some(([start, end]) => from < end && to > start)) {
          unscanned.push(`${file}:${source.slice(0, mention.index).split('\n').length}: ${
            source.slice(mention.index, mention.index + 60).split('\n')[0]}`);
        }
      }
    }
  }

  if (unreadable.length > 0) {
    throw new Error(
      `${unreadable.length} shadow value(s) are introduced through an expression this scan cannot read, `
      + `so nothing checks whether they hold a glow. Inline the value, or name it with a `
      + `--shadow-glow-* token this scan can follow. Do NOT satisfy this by making the channel stop `
      + `matching: a mention that matches nothing at all is caught by the completeness check below, `
      + `while one that matches and yields nothing is scanned and unchecked at once, which is the `
      + `state this exists to make impossible.\n  `
      + unreadable.join('\n  '),
    );
  }

  if (unscanned.length > 0) {
    throw new Error(
      `${unscanned.length} place(s) introduce a shadow value through a spelling no channel of this `
      + `scan reads, so nothing checks whether they hold a glow colour inline. Teach SHADOW_CHANNELS `
      + `the spelling; do NOT narrow SHADOW_MENTION to make this pass, because a mention it stops `
      + `matching is exactly the silent gap these two lists exist to close.\n  `
      + unscanned.join('\n  '),
    );
  }

  if (offenders.length > 0) {
    throw new Error(
      `${offenders.length} shadow layer(s) either draw a glow themselves or cannot be shown not to. `
      + `Spelled-out geometry drifts from design.pen exactly the way a spelled-out colour does: `
      + `neither can be re-anchored for light, and blur, spread and alpha have no scale left to be `
      + `checked against -- so a managed colour does not redeem hand-picked offsets, and neither `
      + `does a tidy name wrapped around them. Pick the token whose blur this is nearest -- dot, `
      + `wire, xs, sm, md, lg, xl or cta -- and use shadow-glow-<role>-<accent>, or `
      + `var(--shadow-glow-<role>-<accent>) as a whole layer inside a composite shadow. Inside `
      + `drop-shadow() only the spreadless roles fit, dot and wire, because that function takes no `
      + `spread and silently drops a layer carrying one. Add the token to @theme if that accent does `
      + `not have one yet. A layer reported as unreadable rather than as a glow is not asking to be `
      + `special-cased: give it a form this scan can classify, or it keeps its own geometry `
      + `unexamined.\n  `
      + offenders.join('\n  '),
    );
  }
}

// Tailwind substitutes an `@theme inline` entry's VALUE into every utility it
// generates from that entry, so the utility never reads the custom property at
// runtime and redeclaring that same token later -- in `:root`, in a light block,
// anywhere -- compiles to nothing. This is not a tidiness rule: it is how
// `--shadow-mint-card` kept the dark frame's neon on a white page while three
// light overrides sat directly above it looking correct. Theming therefore
// happens in the variable an alias POINTS AT, never in the alias, and a dead
// redeclaration now fails here instead of rendering.
function assertInlineThemeTokensAreNotRedeclared(source) {
  const root = postcss.parse(source);
  const inlined = new Set();

  root.walkAtRules('theme', (atRule) => {
    if (!atRule.params.split(/\s+/).includes('inline')) return;
    atRule.walkDecls((decl) => {
      if (decl.prop.startsWith('--')) inlined.add(decl.prop);
    });
  });

  const dead = [];
  root.walkDecls((decl) => {
    if (!inlined.has(decl.prop)) return;
    for (let node = decl.parent; node; node = node.parent) {
      if (node.type === 'atrule' && node.name === 'theme') return;
    }
    dead.push(`line ${decl.source?.start?.line}: ${decl.prop}`);
  });

  if (dead.length > 0) {
    throw new Error(
      `${dead.length} declaration(s) override a token that @theme inline has already substituted into `
      + `its utilities, so they change nothing at all. Point the @theme entry at a runtime variable `
      + `(the shape every other entry uses -- \`--color-mint: var(--mint)\`) and move the override `
      + `onto that variable.\n  `
      + dead.join('\n  '),
    );
  }
}

assertGlowsReadThroughTokens('src');
assertInlineThemeTokensAreNotRedeclared(css);
assertEveryAcceptedRatioStillExists();

const modelHub = (options) => resolveThemeTokens({ ...options, source: modelHubCss });
const modelHubSystemDark = modelHub({ prefersLight: false, themeAttr: null });
const modelHubSystemLight = modelHub({ prefersLight: true, themeAttr: null });
const modelHubExplicitDark = modelHub({ prefersLight: true, themeAttr: 'dark' });
const modelHubExplicitLight = modelHub({ prefersLight: false, themeAttr: 'light' });

assertTokenMapsEqual('model hub system light and explicit light cascade', modelHubSystemLight, modelHubExplicitLight);
assertTokenMapsEqual('model hub system dark and explicit dark cascade', modelHubSystemDark, modelHubExplicitDark);

// The neutral wash channel is white over the dark frame, so any token that reads
// through it renders white-on-white unless light re-anchors the value. Inks are
// the ones that must: a fill or hairline may legitimately just flip channel.
const NEUTRAL_CHANNEL = '--model-hub-wash-channel';
assertEqual(`model hub ${NEUTRAL_CHANNEL} flips per theme`,
  modelHubSystemLight.get(NEUTRAL_CHANNEL) !== modelHubSystemDark.get(NEUTRAL_CHANNEL), true);

const channelInks = [...modelHubSystemDark.entries()]
  .filter(([prop, value]) => prop.includes('ink') && value.includes(NEUTRAL_CHANNEL))
  .map(([prop]) => prop);
assertEqual('model hub ink tokens exist', channelInks.length > 0, true);
for (const prop of channelInks) {
  assertEqual(`model hub ${prop} is re-anchored for light`,
    modelHubSystemLight.get(prop) !== modelHubSystemDark.get(prop), true);
}

console.log('Theme bootstrap and CSS token validation passed.');
