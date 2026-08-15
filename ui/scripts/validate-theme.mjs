import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import postcss from 'postcss';

import { intendedFiles } from './lintPolicy.mjs';

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

// A glow is a shadow layer drawn at the element's own position: both offsets
// zero, a blur, and an accent colour. Written as a literal it is invisible to
// every assertion in this file -- which is how 72 of them accumulated 52
// distinct spellings of the same four shapes, and how the eight card washes
// came to retype `--shadow-mint-card`'s value and so keep the dark frame's
// neon on a white page. A token cannot drift that way: it is one value, it is
// read against design.pen, and light re-anchors it in one place. So a glow
// names a token, and this asserts the property rather than listing the
// spellings, because the next literal will be a spelling nobody listed.
//
// Only the glow layer is held to it. An offset shadow is directional light,
// not a glow, and `0 0 0 2px` is a ring -- no blur, nothing to colour-manage.
// Top-level split, blind to anything inside parentheses. Layers are separated
// by commas in every syntax below; the parts of one layer are separated by
// spaces in CSS and by `_` in a Tailwind arbitrary value, so one splitter reads
// either and no channel needs its own normalisation step.
function splitTopLevel(value, isSeparator) {
  const parts = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < value.length; i += 1) {
    if (value[i] === '(') depth += 1;
    else if (value[i] === ')') depth -= 1;
    else if (depth === 0 && isSeparator(value[i])) {
      parts.push(value.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(value.slice(start));
  return parts.filter((part) => part !== '');
}

const shadowLayers = (value) => splitTopLevel(value, (char) => char === ',');
const layerParts = (layer) => splitTopLevel(layer, (char) => char === '_' || /\s/.test(char));

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
// a token reference so it can be rejected -- never to bless one part of it.
const ZERO_LENGTH = /^0(px|rem|em)?$/;
const LENGTH = /^[+-]?(\d+(\.\d+)?|\.\d+)(px|rem|em|ch|vw|vh)?$/;

// Every channel a shadow value reaches the page through. The first cut of this
// scan read only `shadow-[...]`, which repeated one level up the mistake the
// colour test above was written to stop: it enumerated its own input, so a
// `[box-shadow:...]` written the next day would have walked straight past the
// assertion meant to catch it. Unlike colour syntax this list can genuinely be
// closed -- a shadow is a Tailwind arbitrary value, a Tailwind arbitrary
// property, a CSS declaration or an inline style object, and the stack offers
// no fifth way to write one. But "can be closed" is a claim, so it is measured
// rather than believed: SHADOW_MENTION below finds every place the word appears
// and each one must fall inside a channel, which turns a spelling this file has
// never seen into a loud failure instead of a silent gap.
const SHADOW_CHANNELS = [
  // `shadow-[0_0_16px_-4px_var(--x)]`, including variants such as `hover:shadow-[…]`.
  { pattern: /shadow-\[([^\]]*)\]/g, valuesOf: (match) => [match[1]] },
  // `[box-shadow:0_0_16px_-4px_var(--x)]` -- Tailwind's arbitrary *property*.
  { pattern: /\[box-shadow\s*:([^\]]*)\]/g, valuesOf: (match) => [match[1]] },
  // `box-shadow: 0 0 16px -4px var(--x);` in a stylesheet. The lookbehind hands
  // the arbitrary-property spelling to the channel above rather than matching it
  // twice, once with a stray `]` glued to the colour. A declaration also ends at
  // a quote, because this same spelling appears inside assertion strings in the
  // `.ts` files scanned alongside the CSS -- and a shadow value of its own never
  // contains one, so the quotes cost nothing to stop at and stop the closing one
  // from being read as part of the colour.
  { pattern: /(?<!\[)box-shadow\s*:([^;}'"`]*)/g, valuesOf: (match) => [match[1]] },
  // `style={{ boxShadow: … }}` and `el.style.boxShadow = …`. What follows is an
  // expression rather than a value, so read the string literals out of it: a
  // ternary's two branches are two shadows and both of them render.
  {
    pattern: /boxShadow\s*[:=]([^;\n]*)/g,
    valuesOf: (match) => [...match[1].matchAll(/'([^']*)'|"([^"]*)"|`([^`]*)`/g)]
      .map(([, single, double, template]) => single ?? double ?? template),
  },
];

// A glow is a *value*, and there are only so many ways to introduce one: a `:`
// in a declaration or an object literal, an `=` in an assignment, and Tailwind's
// `shadow-[`. Naming the property without carrying a value cannot hide a glow --
// `transition-[background-color,box-shadow]`, `will-change: box-shadow`, a
// sentence in a comment -- so those are not mentions. What the measurement then
// pins is exactly what failed before: every place a shadow value is INTRODUCED
// is also a place it is READ.
const SHADOW_MENTION = /shadow-\[|box-shadow\s*:|boxShadow\s*[:=]/g;

// Every custom property declared anywhere in the scanned stylesheets, name to
// the set of values it is given. A name is collected once per distinct value
// rather than last-write-wins, because a property is routinely declared several
// times -- dark, `prefers-color-scheme: light`, `[data-theme="light"]` -- and a
// glow smuggled into just one of those blocks is still a glow that ships.
function collectCustomProperties(root) {
  const values = new Map();
  for (const relative of intendedFiles(root, { extensions: ['.css'] })) {
    postcss.parse(fs.readFileSync(path.join(root, relative), 'utf8')).walkDecls((decl) => {
      if (!decl.prop.startsWith('--')) return;
      if (!values.has(decl.prop)) values.set(decl.prop, new Set());
      values.get(decl.prop).add(decl.value);
    });
  }
  return values;
}

const CSS_WIDE_KEYWORDS = new Set(['none', 'inherit', 'initial', 'unset', 'revert', 'revert-layer']);
const GLOW_TOKEN = /^--shadow-glow-/;
const VAR_REFERENCE = /^var\(\s*(--[A-Za-z0-9_-]+)\s*(?:,([\s\S]*))?\)$/;
const MAX_INDIRECTION = 8;

// The classification is DENY BY DEFAULT, and that is the whole point of it.
// Three review rounds each found another spelling that fell past a check built
// to recognise glows and reject them -- an unlisted colour syntax, an unread
// input channel, hand-picked geometry behind a managed colour -- because
// anything the parser failed to recognise landed in an implicit "accept". A
// fourth then found the cheapest fall-through of all: `shadow-[var(--anything)]`
// is a single part, so it had no offsets to test and was waved through, which
// let `--rogue-glow: 0 0 93px var(--mint)` ship a hand-drawn glow behind a name.
// Widening the recogniser a fourth time would just relocate the gap, so the
// default is inverted instead: every layer must land in a form named here, and
// a layer this scan cannot classify FAILS asking to be made legible. A name is
// not taken at face value either -- indirection is resolved and the same test
// runs on what it resolves to, so a glow cannot hide one alias deeper.
function glowOffencesInValue(value, cssVars, seen = new Set(), depth = 0) {
  return shadowLayers(value).flatMap((layer) => glowOffencesInLayer(layer, cssVars, seen, depth));
}

function glowOffencesInLayer(layer, cssVars, seen, depth) {
  const parts = layerParts(layer);
  if (parts[0] === 'inset') parts.shift();
  const shown = layer.trim();

  if (parts.length === 1) {
    const only = parts[0];
    if (CSS_WIDE_KEYWORDS.has(only.toLowerCase())) return [];
    const reference = only.match(VAR_REFERENCE);
    if (!reference) return [`${shown} -- not a length triple, a keyword or a var() this scan can read`];
    const [, name, fallback] = reference;
    if (depth >= MAX_INDIRECTION) return [`${shown} -- indirection deeper than ${MAX_INDIRECTION} hops`];
    if (seen.has(name)) return [];
    const declared = cssVars.get(name);
    if (!declared) return [`${shown} -- ${name} is declared in no scanned stylesheet, so its value cannot be checked`];
    const next = new Set(seen).add(name);
    // The sanctioned home for a shadow literal, and the only stop condition
    // here: a --shadow-glow-* token is read against design.pen and re-anchored
    // for light in one place, which is exactly what a call site cannot do. It
    // stops the recursion, not the check -- the name still has to exist, and a
    // fallback beside it is a second value that renders whenever it does not,
    // so the fallback is classified even when the token it guards is managed.
    const resolved = GLOW_TOKEN.test(name) ? [] : [...declared];
    const deeper = [...resolved, ...(fallback ? [fallback] : [])]
      .flatMap((declaredValue) => glowOffencesInValue(declaredValue, cssVars, next, depth + 1));
    return deeper.map((offence) => `${offence}  <- reached through ${shown}`);
  }

  // CSS lets the colour lead instead of trail, so move a leading non-length to
  // the back and let the offsets line up. This used to exempt a leading `var()`,
  // which quietly reopened the same hole from the other end: `var(--mint) 0 0
  // 93px` kept its colour in the offset slot and drew whatever geometry it liked.
  if (!LENGTH.test(parts[0])) parts.push(parts.shift());
  const [x, y, blur] = parts;
  const isGlow = ZERO_LENGTH.test(x ?? '') && ZERO_LENGTH.test(y ?? '')
    && blur !== undefined && !ZERO_LENGTH.test(blur);
  return isGlow ? [shown] : [];
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
  const cssVars = collectCustomProperties(root);

  for (const relative of intendedFiles(root, { extensions: ['.ts', '.tsx', '.css'] })) {
    const file = path.join(root, relative);
    const source = fs.readFileSync(file, 'utf8');
    const claimed = [];

    for (const { pattern, valuesOf } of SHADOW_CHANNELS) {
      for (const match of source.matchAll(pattern)) {
        claimed.push([match.index, match.index + match[0].length]);

        for (const value of valuesOf(match)) {
          for (const offence of glowOffencesInValue(value, cssVars)) {
            offenders.push(`${file}: ${offence}`);
          }
        }
      }
    }

    for (const mention of source.matchAll(SHADOW_MENTION)) {
      if (!claimed.some(([start, end]) => mention.index >= start && mention.index < end)) {
        unscanned.push(`${file}:${source.slice(0, mention.index).split('\n').length}: ${
          source.slice(mention.index, mention.index + 60).split('\n')[0]}`);
      }
    }
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
      + `does a tidy name wrapped around them. Pick the token whose blur this is nearest -- dot, sm, `
      + `md, lg or cta -- and use shadow-glow-<role>-<accent>, or var(--shadow-glow-<role>-<accent>) `
      + `as a whole layer inside a composite shadow. Add the token to @theme if that accent does not `
      + `have one yet. A layer reported as unreadable rather than as a glow is not asking to be `
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
