import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import postcss from 'postcss';
import ts from 'typescript';

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

// Brand fill/label pairs the owner has accepted against the AA floor. Avibe's
// accents carry the product's personality — an AI-colleague product should read as
// alive, not muted — so the light fills keep design.pen's saturation and the white
// label the design drew, which is the pairing the palette shipped before it was
// briefly deepened. Text on a neutral surface is a separate token (--X-ink) and
// keeps the full floor; only the label printed on a brand fill is accepted here.
//
// These are pinned, not skipped. Each entry records the ratio the pair was accepted
// at and the guard fails when the measured ratio moves off it, so an exemption still
// catches drift: nudge either token and this list goes stale loudly, forcing a fresh
// decision instead of quietly sliding further down. Owner decision 2026-08-14.
const ACCEPTED_BRAND_PAIRS = new Map([
  ['light fill: --primary-foreground on --primary', 2.54],
  ['light fill: --accent-foreground on --accent', 3.68],
  ['light fill: --gold-foreground on --gold', 3.19],
  // Dark violet is not a new exemption: the Button already printed a hard-coded
  // text-white on this fill, so the pair shipped at 4.38:1 while no token declared
  // it and this guard could not see it. Naming --violet-foreground brings it under
  // the same pin as the rest instead of leaving it undeclared.
  ['dark fill: --violet-foreground on --violet', 4.38],
]);
const PIN_TOLERANCE = 0.01;
const consultedBrandPairs = new Set();

function assertContrast(name, tokens, inkProp, surfaceProp) {
  const ink = requireMeasurableColor(name, inkProp, tokens.get(inkProp));
  const surface = requireMeasurableColor(name, surfaceProp, tokens.get(surfaceProp));

  const ratio = contrastRatio(ink, surface);
  const key = `${name}: ${inkProp} on ${surfaceProp}`;
  const accepted = ACCEPTED_BRAND_PAIRS.get(key);
  if (accepted !== undefined) {
    consultedBrandPairs.add(key);
    if (Math.abs(ratio - accepted) > PIN_TOLERANCE) {
      throw new Error(
        `${key} is ${ratio.toFixed(2)}:1, but ACCEPTED_BRAND_PAIRS pins it at ${accepted.toFixed(2)}:1. ` +
          'This pair is exempt from the AA floor by an owner decision taken at that exact ratio, so moving ' +
          'either token needs a fresh decision — re-pin it here once the new value is intended.',
      );
    }
    return;
  }
  if (ratio < AA_SMALL_TEXT) {
    throw new Error(
      `${name}: ${inkProp} on ${surfaceProp} is ${ratio.toFixed(2)}:1, below WCAG AA ${AA_SMALL_TEXT}:1`,
    );
  }
}

// A pair that no longer exists must not keep its exemption: the tokens it excused
// may have been renamed or dropped, and a stale entry would silently excuse whatever
// takes that name next.
function assertEveryAcceptedPairStillExists() {
  const stale = [...ACCEPTED_BRAND_PAIRS.keys()].filter((key) => !consultedBrandPairs.has(key));
  if (stale.length > 0) {
    throw new Error(
      `ACCEPTED_BRAND_PAIRS lists ${stale.join(', ')}, which no theme declares. Drop the entry.`,
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
  // than from ACCENT_FILLS so an ink added on its own is still covered.
  const inks = [...NEUTRAL_INKS, ...[...tokens.keys()].filter((prop) => prop.endsWith('-ink'))];
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
    if (ratio < AA_NON_TEXT) {
      throw new Error(
        `${theme} ring: --ring over ${surfaceProp} is ${ratio.toFixed(2)}:1, below WCAG AA ${AA_NON_TEXT}:1 for non-text`,
      );
    }
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

// Which accent token a mark reaches for is not fill-versus-text. It is whether the
// accent has a LABEL printed on it. A fill or hairline may stay vivid because it is
// read through the paired --X-foreground sitting on it; anything read directly
// against the canvas -- text, an icon, a wire, an end node, a status dot -- carries
// no label and so must take --X-ink. The assertions below cover the places that
// distinction gets lost: a CSS rule painting a wire, a Tailwind utility painting a
// dot, and a TypeScript string smuggling the token past both.
//
// They are needed because neither the ink nor the fill assertion above can see this.
// They check that each token is legible against the surface it claims; they cannot
// know that .model-hub-wire--gateway chose the fill. That is how the supply graph
// shipped 2px --mint strokes at 2.35:1 on the light canvas with every token guard
// passing. Light is where it bites: --mint reads 2.35:1 on --background and --gold
// 2.95:1, both under the 3:1 non-text floor, while their inks clear it. In dark each
// accent's ink IS its fill, so none of this moves a dark pixel.
//
// What these assertions do NOT claim. Each decides fail-closed on what it reads -- an
// unprovable pairing fails, a wash needs a written pin, a bare accent in a TS string is
// rejected outright -- but two of them read an enumeration: Tailwind utility names in
// src/*.tsx, and stroke/fill declarations in the Model Hub stylesheet. An accent can
// reach a pixel by other routes, and five review rounds walked through them one at a
// time. Each route was measured rather than argued about:
//
// Closed here, because closing them was cheap and total. Bare `text-<accent>`
// (INK_UTILITIES) at 0 sites -- the #1406 codemod had already moved all 806 onto -ink,
// which is exactly why the rule belongs here rather than nowhere. And every
// `var(--<accent>)` written into a TypeScript string (assertNoBareAccentVarInSource),
// which had 24 live sites: 1.6px spawn edges in the agent graph, two file-extension icon
// maps, and the editor and terminal active-tab underlines. Those 24 are why the rule is
// a carrier class and not one more utility name -- they reached pixels through a style
// object, an arbitrary value and a lookup table, and no utility name catches any of them.
//
// Zero live sites, so what stays open is a hypothesis rather than a defect: a JSX paint
// attribute (`fill="var(--mint)"`), `<svg color=>` / `stopColor`, an SVG `<animate to>`,
// a runtime `setProperty`, a `:root` alias in a third stylesheet (src/ has exactly two),
// and a conditional label paired with an unconditional fill.
//
// Deliberately out of scope, with the numbers attached: the
// border/ring/outline/shadow/accent utilities. 1000 bare-accent references across 164
// files, 844 of them opacity washes. bg/fill/stroke are already guarded above, which
// leaves 488 on those five utilities -- and only 46 of the 488 are opaque (38 border,
// 3 ring, 2 outline, 3 accent). The 443 washes are decoration whose legibility nobody
// reads, which is not the defect this guard exists for. The 46 opaque ones are a visible
// design change, and recolouring a border is design.pen's call -- the same reason the ten
// ACCEPTED_BARE_FILLS pins are still bare. Both sets are named in the PR's
// known-by-design ledger.
//
// Chasing that last group here would trade a guard people trust for one they route
// around. So the scope is a decision with evidence under it, not an oversight.

const ACCENT_NAMES = ACCENT_FILLS.map((fill) => fill.slice(2));

// `var(--mint)` and `var(--mint, #10b981)` are the same paint. CSS reads the fallback only
// when the name is undefined, and --mint ships in the theme, so the browser paints the
// vivid accent either way and the fallback is decoration on the source. Requiring `)`
// immediately after the name checked one spelling of the reference rather than the
// reference.
//
// Closed with a boundary instead of by parsing the fallback. `(?=[,)])` asserts only that
// the name ended here, which is the single thing this pattern has to get right -- it is
// what separates --mint from --mint-ink. Parsing the argument would have to survive nested
// parens (`var(--mint, rgb(0 0 0))`) and a second comma level, and every version of that
// is another CSS parser living inside a lookahead.
const BARE_ACCENT_VAR = new RegExp(`var\\(\\s*--(${ACCENT_NAMES.join('|')})\\s*(?=[,)])`);

// A wash is not exempt because of what its value looks like. It is exempt because
// someone decided this hairline is decoration, and wrote that down here.
//
// Three revisions tried to infer that decision from the value instead. Matching
// `color-mix` passed an opaque `color-mix(in srgb, var(--mint) 100%, transparent)`.
// Resolving the share and exempting anything under 100% passed a 99% one. Any
// threshold that replaces it can be approached from below, and measuring the
// composited result cannot work either: a real 20% mint wash lands near 1.2:1 on the
// light canvas, so a contrast floor would reject the decorative rails this exemption
// exists for. Whether a translucent stroke is decoration or an illegible mark is
// intent, and intent is not in the CSS.
//
// So the guard stops guessing. Every accent reaching a stroke or fill is either the
// ink, or listed here with a reason. There is no share left to tune and no bypass
// left to find, and the cost is one line per genuine wash -- the same bargain
// ACCEPTED_BARE_FILLS already makes for the control-chrome sites.
//
// Keyed by the resolved declaration, not by its address. An address-keyed pin excuses
// whatever that selector paints next: edit this same stroke from 20% to 100% and an
// opaque mint ships under a pin granted to a hairline. That is the identical defect a
// per-file mark count had -- a pin has to name the construct it was granted for, and
// for a declaration the construct is its value, together with the accents that value
// can reach. Retinting a pinned wash now fails twice over, as an unpinned mark and as
// a stale pin, and the error prints the key to re-pin with.
const ACCEPTED_ACCENT_WASHES = new Map([
  ['model hub .model-hub-rail-line stroke: color-mix(in srgb, var(--mint) 20%, transparent) -> mint', 'decorative rail behind the wires: a 20% tint, not a mark -- the wires it sits under carry the meaning'],
]);

// What a declaration actually paints. A custom property is not a colour, it is a name
// for one, and `--wire-color: var(--mint)` + `stroke: var(--wire-color)` paints bare
// mint while naming neither. modelHubSurface.css already routes ~20 accents through
// --model-hub-* aliases, so this is one refactor away rather than hypothetical.
//
// Every definition of a name counts, not the last one, and every stylesheet that ships
// alongside is a place one can live -- hence the variadic sources. This file defines 11 properties
// twice, once per theme -- `--model-hub-wash-channel` is `8 8 18` in dark and
// `255 255 255` in light -- so a name-to-value map is not a simplification of this
// file, it is a misreading of it. A last-write-wins resolver would read a light
// `stroke: var(--wire)` through a `.dark { --wire: ... }` override and clear it on the
// strength of a value the browser never paints there.
//
// Resolving the cascade for real means specificity, at-rules and order: a CSS engine,
// inside a guard, and one whose bugs would be silent clears. The guard does not need
// one, because it is not asking what colour this paints. It is asking whether ANY
// definition can put a bare accent here -- so the union over all of them is the
// answer, ambiguity fails closed, and a theme-specific accent gets reported instead of
// averaged away.
//
// Accents reachable from a name, memoised, with the in-progress entry doubling as the
// cycle cut: CSS permits `--a: var(--b); --b: var(--a)`, and a resolver that trusts
// its input would spin. Cutting the cycle can under-report a cyclic definition, which
// is correct rather than a hole -- a var() cycle is invalid at computed-value time, so
// the browser paints nothing there either.
function accentAliasIndex(...sources) {
  const definitions = new Map();
  for (const source of sources) {
    postcss.parse(source).walkDecls((decl) => {
      if (decl.prop.startsWith('--')) {
        const values = definitions.get(decl.prop) ?? new Set();
        values.add(normalizeCssValue(decl.value));
        definitions.set(decl.prop, values);
      }
    });
  }

  const reached = new Map();

  const accentsOfName = (name) => {
    if (reached.has(name)) {
      return reached.get(name);
    }
    reached.set(name, new Set());
    const found = new Set();
    for (const value of definitions.get(name) ?? []) {
      for (const accent of accentsOfValue(value)) {
        found.add(accent);
      }
    }
    reached.set(name, found);
    return found;
  };

  function accentsOfValue(value) {
    const found = new Set();
    for (const [, name] of value.matchAll(/var\(\s*(--[\w-]+)\s*\)/g)) {
      if (ACCENT_NAMES.includes(name.slice(2))) {
        found.add(name.slice(2));
      } else {
        for (const accent of accentsOfName(name)) {
          found.add(accent);
        }
      }
    }
    return found;
  }

  return accentsOfValue;
}

function assertMarksTakeTheInk(source, name, ...aliasSources) {
  const accentsOfValue = accentAliasIndex(source, ...aliasSources);
  const seen = new Set();

  postcss.parse(source).walkDecls((decl) => {
    if (decl.prop !== 'stroke' && decl.prop !== 'fill') {
      return;
    }

    const value = normalizeCssValue(decl.value);
    const accents = [...accentsOfValue(value)].sort();
    if (accents.length === 0) {
      return;
    }

    const selector = decl.selector ?? decl.parent?.selector;
    // The pin names the declaration as written AND what it can reach. Value alone
    // would let `--wire: var(--mint-ink)` become `var(--mint)` under a pin granted to
    // the ink, since `stroke: var(--wire)` reads the same either way.
    const key = `${name} ${selector} ${decl.prop}: ${value} -> ${accents.join(', ')}`;
    seen.add(key);
    if (ACCEPTED_ACCENT_WASHES.has(key)) {
      return;
    }

    const direct = BARE_ACCENT_VAR.test(value);
    throw new Error(
      `${name}: ${selector} paints ${decl.prop} with ${accents.map((accent) => `var(--${accent})`).join(' and ')}`
      + `, the fill${direct ? '' : ` -- reached through ${value}, in this file or a theme override of it`}. `
      + 'A stroke or an SVG fill is a mark on the bare canvas -- no label is printed on it, so the '
      + 'pairing that licenses a vivid fill does not apply. Use the -ink token, which is the same value '
      + 'in dark and a legible one in light. If this one is decoration rather than a mark, say so in '
      + `ACCEPTED_ACCENT_WASHES under '${key}' -- a translucent value is not evidence on its own.`,
    );
  });

  return seen;
}

// index.css comes in as a definition source, not as a scan target. A custom property is
// global once it is on :root, so `--wire-color: var(--mint)` in the app stylesheet is
// visible to `stroke: var(--wire-color)` here -- the browser resolves it across files and
// an index built from this file alone would see no definition and clear the wire. The
// union is the answer for the same reason it is within one file: the question is whether
// ANY definition can put a bare accent on this stroke.
const accentPaints = assertMarksTakeTheInk(modelHubCss, 'model hub', css);

// A pin that outlives what it excused is a silent exemption, so every entry has to
// match a declaration that is really there.
for (const [key, reason] of ACCEPTED_ACCENT_WASHES) {
  if (!accentPaints.has(key)) {
    throw new Error(
      `ACCEPTED_ACCENT_WASHES pins '${key}' (${reason}), but no stroke or fill paints that any more. `
      + 'Drop the pin along with the declaration it was granted for -- or, if the declaration is still '
      + 'there with a different value, re-pin the new one and re-read the reason against it.',
    );
  }
}

// The same rule in Tailwind: `bg-mint` on a 6px dot is a mark, `bg-mint` under a
// `text-primary-foreground` label is a fill. Only the label tells them apart, so the
// question is where a label counts as belonging to this fill. Two places do, and they
// are the two the code actually uses:
//
//   1. the same class string -- `bg-mint text-primary-foreground` (21 of the 23
//      labelled fills in src/, including every cva variant in button-variants.ts)
//   2. a sibling attribute or property of the same node -- an icon tile passes
//      `iconTileClassName="bg-gold"` beside `iconClassName="text-gold-foreground"`,
//      and agentBackends.ts pairs `tileCls`/`iconCls` in one object literal
//
// and the label must also apply whenever the fill does. A label restricted to a state
// the fill is not -- `bg-mint hover:text-primary-foreground` -- leaves the resting
// element unlabeled, so the variant prefix has to match or be absent. Absent is the
// broader case and passes: an always-on label still labels a hover-only fill.
//
// Both scopes come from the TypeScript AST, so "belongs to" is a parent node rather
// than a line count. Proximity is not evidence: an earlier draft looked in a +/-4 line
// window and would accept a `text-primary-foreground` button standing next to an
// unrelated `bg-mint` dot as proof the dot was labelled -- passing exactly the mark it
// exists to catch. Reading literals off the AST also drops the need to blank comments
// (a class name in JSDoc is comment trivia, never a string literal) and makes
// multi-line class strings work without a hand-rolled quote scanner.
//
// A label on a child element (`<div className="bg-mint"><span
// className="text-primary-foreground">`) is deliberately NOT a scope: no site needs it
// today, and widening to a subtree would re-admit an unrelated nested label. Such a
// site fails and is either merged into one class string or pinned below.
//
// mint/cyan/pink declare no -foreground of their own; they print --primary-foreground
// and --accent-foreground, the aliases SEMANTIC_FILL_ALIASES keeps equal. pink has no
// label token at all, so a bare bg-pink can only ever be an unlabeled mark.
const FILL_LABEL = new Map([
  ['primary', 'primary-foreground'],
  ['mint', 'primary-foreground'],
  ['accent', 'accent-foreground'],
  ['cyan', 'accent-foreground'],
  ['destructive', 'destructive-foreground'],
  ['violet', 'violet-foreground'],
  ['gold', 'gold-foreground'],
]);

// Control chrome the owner has not ruled on: switch/toggle tracks and progress/step
// bars, where the fill is a large shape whose state is already carried by knob
// position or bar length rather than by the colour being legible on its own.
// Repainting them is a design.pen decision, not a guard decision.
//
// Each is pinned to the construct it was granted for -- the owning component plus the
// exact class string carrying the mark -- not to a per-file tally. A count treats every
// same-accent mark in a file as interchangeable: delete the pinned track, add an
// unrelated bare dot, and `1` still reads as `1`, so the exemption transfers in silence
// to a mark nobody ruled on. A signature cannot transfer. It also survives edits above
// it and reformatting, which a line number would not, and reads as documentation of
// what is actually exempt.
const TRACK = 'toggle track: state is carried by knob position, not by the fill reading on its own';
const STEP_DOTS = 'wizard step dots: progress is carried by how many are filled, not by one dot reading on its own';
const STEP_DOT = 'bg-mint shadow-[0_0_8px_rgba(91,255,160,0.6)]';
const ACCEPTED_BARE_FILLS = new Map([
  ['src/components/ui/switch.tsx --primary', { reason: TRACK, marks: ['Switch: bg-primary'] }],
  ['src/components/settings/SettingsPrimitives.tsx --mint', { reason: TRACK, marks: ['ToggleSwitch: border-mint/50 bg-mint shadow-[0_0_12px_-2px_rgba(91,255,160,0.6)]'] }],
  ['src/components/steps/PlatformSelection.tsx --mint', { reason: TRACK, marks: ['CredentialToggle: bg-mint'] }],
  ['src/components/visual/ProgressBar.tsx --mint', { reason: 'progress bar: progress is carried by bar length', marks: ['ProgressBar: bg-mint shadow-[0_0_12px_rgba(91,255,160,0.45)]'] }],
  ['src/components/steps/SlackConfig.tsx --mint', { reason: STEP_DOTS, marks: [`SlackConfig: ${STEP_DOT}`] }],
  ['src/components/steps/TelegramConfig.tsx --mint', { reason: STEP_DOTS, marks: [`TelegramConfig: ${STEP_DOT}`] }],
  ['src/components/steps/DiscordConfig.tsx --mint', { reason: STEP_DOTS, marks: [`DiscordConfig: ${STEP_DOT}`] }],
  ['src/components/steps/LarkConfig.tsx --mint', { reason: STEP_DOTS, marks: [`LarkConfig: ${STEP_DOT}`] }],
  ['src/components/steps/WeChatConfig.tsx --mint', { reason: STEP_DOTS, marks: [`WeChatConfig: ${STEP_DOT}`] }],
  ['src/components/visual/EmbeddedConfigShell.tsx --mint', { reason: STEP_DOTS, marks: [`EmbeddedConfigShell: ${STEP_DOT}`] }],
]);

// What an exemption is bound to. Kept as one string so the pins above read as the
// construct they excuse.
const markSignature = (owner, text) => `${owner}: ${text}`;

// Every class string in a file, paired with the scope a sibling label may live in:
// the JSX attribute list or the object literal the string sits in, else the string
// itself. A template literal counts as one class string -- its static head and spans
// are one `className`, and the interpolations are visited separately.
//
// The dialect comes from the extension. A .ts file forced through the TSX parser reads a
// generic arrow (`const f = <T>(x: T) => x`) as an unterminated JSX element: nine files in
// src/ do that today, and asyncLifetime.ts surfaced 1 of its 53 string literals, so an
// unlabeled bg-mint anywhere past the first generic would have passed unseen.
//
// Parse diagnostics are raised rather than ignored, because ignoring them is what made
// that invisible: a file the parser cannot read yields no class strings, which is
// indistinguishable from a clean file. The scan must not be able to go blind quietly.
function classStrings(file, source) {
  const tsx = file.endsWith('.tsx');
  const ast = ts.createSourceFile(
    file, source, ts.ScriptTarget.Latest, true, tsx ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const [failure] = ast.parseDiagnostics;
  if (failure) {
    const { line } = ast.getLineAndCharacterOfPosition(failure.start);
    throw new Error(
      `${file}:${line + 1} does not parse as ${tsx ? 'TSX' : 'TypeScript'}: `
      + `${ts.flattenDiagnosticMessageText(failure.messageText, ' ')}\n`
      + 'A file the parser cannot read contributes no class strings, so every accent mark in it '
      + 'would skip this check unseen. Fix the syntax, or give this scan the dialect it needs.',
    );
  }
  const strings = [];

  // An object literal is a shared scope only when the class string is a property
  // VALUE, which is what a config map looks like: `{ tileCls: 'bg-gold', iconCls:
  // 'text-gold-foreground' }` describes one node, so the two pair. As a property KEY
  // it is a clsx condition branch instead -- `clsx({ 'bg-mint': a, 'text-primary-
  // foreground': b })` -- and branches that toggle independently are the opposite of
  // evidence: the fill can render while the label's condition is false. Reading which
  // side of the colon the string sits on separates them without an allowlist of
  // helper names.
  const scopeOf = (node) => {
    const parent = node.parent;
    if (ts.isJsxAttribute(parent) || (ts.isJsxExpression(parent) && ts.isJsxAttribute(parent.parent))) {
      return ts.isJsxAttribute(parent) ? parent.parent : parent.parent.parent;
    }
    if (ts.isPropertyAssignment(parent) && parent.initializer === node
      && ts.isObjectLiteralExpression(parent.parent)) {
      return parent.parent;
    }
    return node;
  };

  // The nearest named declaration around a class string -- the component or helper the
  // mark lives in. Anonymous arrows and JSX nodes in between are skipped, so a mark
  // inside a `.map()` callback still reports the component. Half of what a pinned
  // exemption is bound to; see markSignature.
  const ownerOf = (node) => {
    for (let current = node; current; current = current.parent) {
      if (ts.isVariableDeclaration(current) && ts.isIdentifier(current.name)) {
        return current.name.text;
      }
      if ((ts.isFunctionDeclaration(current) || ts.isClassDeclaration(current)
        || ts.isMethodDeclaration(current)) && current.name) {
        return current.name.getText(ast);
      }
    }
    return '(module)';
  };

  // The sibling class strings that apply whenever the marked one does -- which is not
  // the same as every string in the opening element. A label reached only through a
  // condition (`active ? 'text-mint-foreground' : ''`, `active && '...'`) leaves an
  // unlabeled resting element behind, and that resting state is exactly what the guard
  // is asked about. Raw source text cannot tell the two apart, so it cleared a mark on
  // the strength of a branch that may never be taken -- the one direction a fail-closed
  // guard must not fail.
  //
  // Both operands of a gate are excluded, not just the fallback. A label written into
  // every branch does apply unconditionally, but proving that means evaluating the
  // condition; the guard fails closed instead and the fix is to merge the label into the
  // fill's own string, which is where it belonged anyway.
  const unconditionalStrings = (scope) => {
    const parts = [];
    const walk = (node, gated) => {
      if (!gated) {
        if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
          parts.push(node.text);
        } else if (ts.isTemplateExpression(node)) {
          parts.push(node.head.text, ...node.templateSpans.map((span) => span.literal.text));
        }
      }
      if (ts.isConditionalExpression(node)) {
        walk(node.condition, gated);
        walk(node.whenTrue, true);
        walk(node.whenFalse, true);
        return;
      }
      if (ts.isBinaryExpression(node) && [
        ts.SyntaxKind.AmpersandAmpersandToken,
        ts.SyntaxKind.BarBarToken,
        ts.SyntaxKind.QuestionQuestionToken,
      ].includes(node.operatorToken.kind)) {
        walk(node.left, true);
        walk(node.right, true);
        return;
      }
      ts.forEachChild(node, (child) => walk(child, gated));
    };
    walk(scope, false);
    return parts.join(' ');
  };

  const record = (node, text) => {
    strings.push({
      text,
      owner: ownerOf(node),
      scope: unconditionalStrings(scopeOf(node)),
      line: ast.getLineAndCharacterOfPosition(node.getStart(ast)).line + 1,
    });
  };

  const visit = (node) => {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      record(node, node.text);
    } else if (ts.isTemplateExpression(node)) {
      record(node, [node.head.text, ...node.templateSpans.map((span) => span.literal.text)].join(' '));
      node.templateSpans.forEach((span) => visit(span.expression));
      return;
    }
    ts.forEachChild(node, visit);
  };

  visit(ast);
  return strings;
}

// Which utilities paint an accent onto a shape. `bg-` is the common one, but an icon
// is a mark too, and Tailwind colours an SVG with `fill-`/`stroke-`: AppsLauncher's
// pinned-state Pin shipped a 14px `fill-cyan` glyph that this scan could not see.
// A 14px filled glyph is read like small text rather than like a large non-text shape,
// which is the bar its ink clears and its fill does not.
// A gradient stop is a fill too, spelled in three parts. `from-mint via-mint to-mint`
// paints the same shape `bg-mint` does, so leaving them out would have been a hole with
// a Tailwind name -- 0 sites today, which is why they cost nothing to close now instead
// of in the round that finds them.
//
// `border-`, `ring-`, `outline-` and `divide-` are deliberately NOT here. They paint a
// hairline, not a shape, and whether a 1px accent border needs the ink is the same open
// design.pen question as the ten pinned control-chrome sites -- 46 live sites, measured
// and recorded in the PR ledger rather than quietly recoloured to satisfy this file. That
// is a decision, not an omission; do not read the gap as one and "fix" it here.
const MARK_UTILITIES = ['bg', 'fill', 'stroke', 'from', 'via', 'to'];

// `text-mint` is not a fill that might carry a label. It IS the label -- and through
// `currentColor`, so is every icon under it that names no colour of its own, which is
// how one class on a wrapper tints a whole glyph. So the pairing that licenses `bg-mint`
// cannot license it: nothing is printed on top of text, and there is no second token to
// look for. The answer is always the ink, which is the same value in dark and a legible
// one in light -- so unlike a toggle track, this rule takes no exemption. A vivid accent
// used as text has no reading under which it was intended.
//
// Zero sites violate this today; the #1406 codemod moved all 806 onto -ink (240 mint,
// 214 cyan, 138 destructive, 115 gold, 58 violet, 28 pink, 12 accent, 1 primary). That
// is exactly why the rule belongs here: the codemod is what made it true, and nothing
// was stopping the next `text-mint` from undoing it one site at a time.
// `placeholder-`, `decoration-` and `caret-` print ink for the same reason: a placeholder
// is text, an underline is read as part of the glyphs it sits under, and a caret you
// cannot find is a text field you cannot use. 0 sites today.
//
// The flag is which of them prints *glyphs*, because "is ink" and "can carry a label" are
// two different questions and collapsing them opens the hole this file just closed on
// `border-`: a `caret-primary-foreground` is a 1px bar, so licensing `bg-mint` with it
// excuses the fill on the strength of something nobody reads a word off. Glyphs sit on
// the fill and are read off it; a caret and an underline sit near text without being it.
const INK_UTILITIES = new Map([
  ['text', true],
  ['placeholder', true],
  ['decoration', false],
  ['caret', false],
]);
const LABEL_UTILITIES = [...INK_UTILITIES]
  .filter(([, printsGlyphs]) => printsGlyphs)
  .map(([utility]) => utility);

// `bg-mint/100` is not a wash. It is `bg-mint` spelled with a modifier -- same token,
// same opacity, same 2.35:1 -- and a `/` in the lookahead let it leave the scan
// entirely. So the modifier is read rather than treated as an exit: fully opaque is
// the fill, and a genuinely translucent one is a derived colour this guard does not
// reason about, the same way it does not reason about `bg-mint-soft`.
//
// This is deliberately NOT the CSS half's rule, where a wash needs a written pin. The
// asymmetry is in the inventory, not in the principle. modelHubSurface.css has one
// translucent accent paint, so pinning it costs one line; `src/` has ~370
// `bg-<accent>/10`-style washes sitting behind content. 370 pins would not make
// anything legible, it would make this guard something people route around, and a
// guard that is routed around checks nothing. The two cases also fail differently:
// a translucent mark is invisible in both themes, which whoever writes it sees
// immediately, while an opaque one looks right in dark and goes illegible only in
// light -- unseen, which is the whole reason this guard exists.
// So the line is drawn on the alpha, which means reading it as a number. Tailwind
// spells the same alpha four ways -- `/70`, `/[0.7]`, `/[70%]`, and no modifier at all
// -- and matching the spellings that mean 1 exempted `bg-mint/[99%]`, which is the same
// pixel as `bg-mint`. An enumeration of opaque spellings can only ever be as complete as
// whoever wrote it; a parsed number is complete by construction.
//
// Unparseable returns null rather than a number, and the callers fail closed in opposite
// directions: an unreadable mark must still be checked, an unreadable label must not license
// one. Collapsing both onto 1 -- which is what "opaque" means -- reads as fail-closed and is
// only half of one, because the same 1 that says "check this fill" also says "this label is
// printed". That asymmetry is not hypothetical: reusing this number for the label is what
// round 7 did, and it is exactly where the previous round's hole was.
const markAlpha = (modifier) => {
  if (!modifier) {
    return 1;
  }
  const raw = modifier.slice(1).replace(/^\[|\]$/g, '');
  const percent = raw.endsWith('%');
  const value = Number.parseFloat(percent ? raw.slice(0, -1) : raw);
  if (!Number.isFinite(value) || value < 0) {
    return null;
  }
  return percent || value > 1 ? value / 100 : value;
};

// Where a tint stops being a tint. The exemption above says a wash is not a mark -- it
// is a colour cast on the surface, and what gets read is whatever sits on top of it. At
// alpha 0.9 that defence is gone: the composite differs from the opaque fill by a tenth
// of the surface, so it is the same shape, read the same way, and `/[99%]` was using the
// wash exemption to smuggle it through.
//
// 0.5-0.89 is a real grey zone, and the tree has 35 of them (`/50`, `/55`, `/60`, `/70`).
// Where a half-opacity accent stops being decorative is a design.pen question, not a
// guard question, so they stay out and go in the ledger rather than being recoloured to
// satisfy this file. The threshold is the honest instrument: one stated number, with the
// parsed alpha reported in the error, instead of a list that silently grows a hole.
const OPAQUE_ENOUGH = 0.9;

// Does the alpha this guard read mean "check this as a fill"? Named and shared rather than
// written inline at the scan, because a mutation sweep found the inline form untested: the
// corpus below could see what the alphabet READ and not what the scan DECIDED, so flipping
// this one condition to fail open passed everything. Same shape as the defect this round is
// closing, one level down -- a rule with no single owner is a rule with no test.
// The two differ in exactly one place -- what an unreadable alpha means -- and that is the
// asymmetry worth having in one line where both can be seen at once: an alpha this guard
// could not parse must still be checked as a fill, and must not be accepted as a label.
const readsAsTheFill = (alpha) => alpha === null || alpha >= OPAQUE_ENOUGH;
const printsTheLabel = (alpha) => alpha !== null && alpha >= OPAQUE_ENOUGH;

// How a utility names an accent. Tailwind v4 admits four spellings that compile to the
// same paint -- `bg-mint`, the custom-property shorthand `bg-(--mint)`, and the
// arbitrary-value forms `bg-[var(--mint)]` and `bg-[--mint]` -- and this file was reading
// the first one.
//
// One source, because the missing spellings were the symptom and this is the defect: "does
// this text name accent X as paint, and how opaquely" was answered by three separate
// patterns -- the mark scan, the var scan, and a substring search for the label -- so a
// spelling only had to be missed by ONE of them to ship, and nothing made a new spelling
// reach the other two. Three review rounds each found a different member of that set. A
// shared alphabet is what turns the next spelling into one row instead of three edits, and
// ACCENT_SPELLINGS below is what makes a broken row a failure instead of a silent hole.
//
// Deliberately loose about the wrapper. This alphabet decides only what gets CHECKED, so an
// invented spelling costs a redundant check while a missing one ships an unlabeled fill --
// the asymmetry says to over-match. The one place it must be exact is the end of the token:
// `(?![\w-])` is why `bg-mint-ink` and `text-primary-foregroundish` are still different
// words, and why matching the remedy this guard prints does not reject the fix.
//
// Contributes two groups, in order: the token, then the opacity modifier. The modifier is
// read after the wrapper closes, because `bg-(--mint)/70` closes its bracket first.
const accentTokenSource = (tokens) => (
  `-(?:\\[var\\(\\s*|\\[|\\(\\s*)?(?:--)?(${tokens})(?![\\w-])(?:\\s*\\)|\\])*(/[\\w.%[\\]]+)?`
);

// Built per call, not shared: a `g` regex carries `lastIndex`, so one instance handed to
// two scans would start the second one wherever the first stopped. The factory is what lets
// the corpus test the pattern the scan actually uses rather than a copy of it.
const accentMarkPattern = () => new RegExp(
  `\\b(${[...MARK_UTILITIES, ...INK_UTILITIES.keys()].join('|')})`
  + accentTokenSource([...FILL_LABEL.keys(), 'pink'].join('|')),
  'g',
);

// The Tailwind state the utility containing `index` is gated behind: `hover:`,
// `md:hover:`, or '' for none. Scoped to the one utility rather than the class string,
// so two utilities sharing a string can be compared. Any whitespace separates
// utilities, because a multi-line class string wraps on newlines.
const variantPrefix = (text, index) => {
  let start = index;
  while (start > 0 && !/\s/.test(text[start - 1])) {
    start -= 1;
  }
  const colon = text.lastIndexOf(':', index - 1);
  return colon >= start ? text.slice(start, colon + 1) : '';
};

// Does this label apply wherever the fill does? An unprefixed label always applies,
// so it labels a `hover:`-only fill. Anything else has to match exactly. A broader but
// unequal chain (label `hover:`, fill `md:hover:`) fails rather than being reasoned
// about: no site writes one, and a guard should fail closed on a shape it cannot
// prove -- the fix is to merge the two utilities into one string, or pin the mark.
// A label also has to BE a label, which a substring search cannot tell you.
// `text-primary-foreground/0` is the right token, spelled completely, applying in the right
// scope -- and invisible; `text-primary-foregroundish` is not the token at all. Both cleared
// the mark. So the label is read through the same alphabet and the same alpha as the mark it
// licenses: a pairing is a claim about contrast between two painted things, and something
// unpainted cannot make it.
//
// One threshold serves both sides on purpose. A second number for labels would have to come
// from somewhere -- white at 0.7 over vivid mint is still perfectly legible, so the honest
// label threshold is not 0.9 -- and a number nobody can derive is how a tunable becomes a
// hole to be argued down. At or above 0.9 a label is the label; below it, whether a
// translucent label carries a vivid fill is a design.pen question, and the answer is a pin
// with a reason rather than a threshold quietly lowered to admit it. Nothing live is
// affected: all 23 label sites in src/ are bare.
const labelCovers = (haystack, utility, label, fillPrefix) => {
  const printed = new RegExp(`\\b${utility}${accentTokenSource(label)}`, 'g');
  for (const match of haystack.matchAll(printed)) {
    if (!printsTheLabel(markAlpha(match[2]))) {
      continue;
    }
    const prefix = variantPrefix(haystack, match.index);
    if (prefix === '' || prefix === fillPrefix) {
      return true;
    }
  }
  return false;
};

// Every TypeScript source under a root, in a stable order. Shared by the two scans
// below so the normalisation cannot drift between them.
//
// readdirSync(recursive) yields platform separators, so on Windows an entry arrives
// as `components\ui\switch.tsx`. Normalise before it becomes a lookup key, or every
// pin below misses and validate:theme fails on an unchanged checkout.
function sourceFiles(root) {
  return fs.readdirSync(root, { recursive: true })
    .map((entry) => entry.split(path.sep).join('/'))
    .filter((entry) => /\.tsx?$/.test(entry))
    .map((entry) => `${root}/${entry}`)
    .sort();
}

// The alphabet's own test, run before any file is read. If the pattern the scans are about
// to use has stopped recognising a spelling, that is a hole in this guard, and it should
// fail here -- next to the row that names the spelling -- instead of quietly passing a tree
// that contains one. Adding a spelling means adding a row, which is the point of there being
// one alphabet at all.
//
// The negatives carry as much weight as the positives, and each was a real defect or a real
// near-miss. `bg-mint-ink` is the remedy this guard prints, so matching it would reject the
// fix. `bg-mint/10` is the wash exemption, and it is here as a number rather than as an
// absence, because the bug it replaced was a `/` treated as an exit from the scan.
// `text-primary-foreground` must not read as a bare `primary` mark -- that boundary broke
// twice. `ring-mint` is the deliberate scope decision, not an oversight; the comment above
// ACCENT_NAMES carries the count behind it.
// Columns: the spelling, the accent it names (null for "not a mark"), the alpha this guard
// reads, and whether that alpha reads as the fill. The last column is written out rather than
// derived, so a flip in readsAsTheFill fails here instead of only in an out-of-tree probe.
const ACCENT_SPELLINGS = [
  ['bg-mint', 'mint', 1, true],
  ['bg-mint/95', 'mint', 0.95, true],
  ['bg-mint/[99%]', 'mint', 0.99, true],
  ['bg-mint/[0.95]', 'mint', 0.95, true],
  ['bg-mint/10', 'mint', 0.1, false],
  ['bg-mint/[oops]', 'mint', null, true],
  ['bg-(--mint)', 'mint', 1, true],
  ['bg-(--mint)/10', 'mint', 0.1, false],
  ['bg-(--mint)/[99%]', 'mint', 0.99, true],
  ['bg-[var(--mint)]', 'mint', 1, true],
  ['bg-[--mint]', 'mint', 1, true],
  ['hover:bg-mint', 'mint', 1, true],
  ['fill-cyan', 'cyan', 1, true],
  ['stroke-gold', 'gold', 1, true],
  ['text-violet', 'violet', 1, true],
  ['caret-pink', 'pink', 1, true],
  ['bg-mint-ink', null, null, null],
  ['bg-mint-foreground', null, null, null],
  ['text-primary-foreground', null, null, null],
  ['bg-muted', null, null, null],
  ['ring-mint', null, null, null],
  ['auto-mint', null, null, null],
];

// The same list for the other operand. A label is only a label when it prints the token as
// glyphs, opaquely, in a scope the fill also applies to -- so these rows are the three ways
// that can be false, plus the spellings that make it true.
const LABEL_SPELLINGS = [
  ['bg-mint text-primary-foreground', true],
  ['bg-mint text-primary-foreground/90', true],
  ['bg-mint text-primary-foreground/[95%]', true],
  ['bg-mint text-(--primary-foreground)', true],
  ['bg-mint text-[var(--primary-foreground)]', true],
  ['bg-mint text-primary-foreground/0', false],
  ['bg-mint text-primary-foreground/50', false],
  ['bg-mint text-primary-foregroundish', false],
  ['bg-mint text-primary-foreground/[oops]', false],
  ['bg-mint hover:text-primary-foreground', false],
];

// And the third operand. The var scan reads a CSS reference rather than a utility, so its
// spellings are its own -- and it had no rows here until reverting its fix ran this corpus
// green, which is the same defect one level up: a shared question with one operand left
// outside the shared test. That is how round 6 shipped a measured mark next to an unmeasured
// label, so the rule now is that every predicate answering "does this name an accent" owes
// this file rows.
const VAR_SPELLINGS = [
  ['var(--mint)', 'mint'],
  ['var(--mint, #10b981)', 'mint'],
  ['var(--mint,#fff)', 'mint'],
  ['var(--mint, rgb(0 0 0))', 'mint'],
  ['var( --cyan )', 'cyan'],
  ['var(--mint-ink)', null],
  ['var(--mint-foreground)', null],
  ['var(--mint-ink, #063)', null],
];

function assertAccentSpellingsAreCovered() {
  for (const [text, accent, alpha, checked] of ACCENT_SPELLINGS) {
    const read = [...text.matchAll(accentMarkPattern())].map((match) => {
      const value = markAlpha(match[3]);
      return [match[2], value, readsAsTheFill(value)];
    });
    const expected = accent === null ? [] : [[accent, alpha, checked]];
    if (JSON.stringify(read) !== JSON.stringify(expected)) {
      throw new Error(
        `The accent alphabet reads "${text}" as ${JSON.stringify(read)}, expected ${JSON.stringify(expected)}.\n`
        + 'ACCENT_SPELLINGS is the set of spellings this guard promises to see; a row that stops '
        + 'holding is a hole in the scans below, not a stale expectation.',
      );
    }
  }

  for (const [text, labelled] of LABEL_SPELLINGS) {
    if (labelCovers(text, 'text', 'primary-foreground', '') !== labelled) {
      throw new Error(
        `The label alphabet reads "${text}" as ${!labelled}, expected ${labelled}.\n`
        + `A label counts only when it prints the token as glyphs at alpha ${OPAQUE_ENOUGH} or above, `
        + 'in a scope the fill also applies to.',
      );
    }
  }

  for (const [text, accent] of VAR_SPELLINGS) {
    const [, read = null] = text.match(BARE_ACCENT_VAR) ?? [];
    if (read !== accent) {
      throw new Error(
        `The var alphabet reads "${text}" as ${JSON.stringify(read)}, expected ${JSON.stringify(accent)}.\n`
        + 'A reference names the accent whatever follows the token, and names a different token when '
        + 'the name continues -- --mint-ink is not --mint with a suffix.',
      );
    }
  }
}

function assertUnlabeledFillsTakeTheInk(root) {
  const bare = accentMarkPattern();
  const found = new Map();
  const inkMarks = [];

  for (const file of sourceFiles(root)) {
    for (const { text, owner, scope, line } of classStrings(file, fs.readFileSync(file, 'utf8'))) {
      for (const match of text.matchAll(bare)) {
        const utility = match[1];
        const accent = match[2];
        const modifier = match[3];
        // Only a modifier this guard could READ exempts a wash. One it could not read is the
        // spelling nobody predicted, so the mark stays in and the error says why.
        const alpha = markAlpha(modifier);
        if (!readsAsTheFill(alpha)) {
          continue;
        }
        // Reported alongside the site: a `/[99%]` mark was written believing it was a
        // wash, so the number this guard read is the one fact that explains the failure.
        let note = '';
        if (alpha === null) {
          note = ` (opacity modifier ${modifier} does not parse, so it is read as the fill)`;
        } else if (alpha < 1) {
          note = ` (alpha ${alpha}, at or above ${OPAQUE_ENOUGH} reads as the fill)`;
        }
        // Ink takes no label and no pin, so it never reaches the pairing logic below.
        if (INK_UTILITIES.has(utility)) {
          inkMarks.push({ file, line, utility, accent, note, signature: markSignature(owner, text) });
          continue;
        }
        // A label only labels if it is printed as ink. The lookup was for the token
        // suffix anywhere in the string, so `bg-mint border-primary-foreground` cleared
        // the mark on the strength of a 1px border nobody reads a word off -- the token
        // was present, the label was not. The utilities that print ink are already
        // named, so the label has to be carried by one of them.
        const label = FILL_LABEL.get(accent);
        const labelled = label !== undefined && LABEL_UTILITIES.some((ink) => {
          const fillPrefix = variantPrefix(text, match.index);
          return labelCovers(text, ink, label, fillPrefix) || labelCovers(scope, ink, label, fillPrefix);
        });
        if (labelled) {
          continue;
        }

        const key = `${file} --${accent}`;
        found.set(key, [...found.get(key) ?? [], { signature: markSignature(owner, text), line, note }]);
      }
    }
  }

  if (inkMarks.length > 0) {
    throw new Error(
      'These paint a glyph, a placeholder, an underline or the caret with a bare accent -- text, '
      + 'plus through currentColor every icon inside it. That is the ink side of the split by '
      + 'definition: nothing can sit on top of text, so there is nothing to pair it with. Use the '
      + '-ink token:\n'
      + inkMarks.map(({ file, line, utility, accent, note, signature }) => (
        `  ${file}:${line} ${utility}-${accent} -> ${utility}-${accent}-ink${note}\n    ${signature}`
      )).join('\n'),
    );
  }

  const unpinned = [...found.entries()].filter(([key]) => !ACCEPTED_BARE_FILLS.has(key));
  if (unpinned.length > 0) {
    throw new Error(
      'These paint a bare accent with no paired *-foreground label in the same class string or in an '
      + 'unconditional sibling attribute/property, so they are marks read straight off the canvas and must use the '
      + '-ink token (bg-mint-ink, fill-cyan-ink, stroke-gold-ink, ...):\n'
      + unpinned.map(([key, marks]) => marks.map(({ signature, line, note }) => `  ${key} at line ${line}${note}\n    ${signature}`).join('\n')).join('\n')
      + `\nA label counts only when one of ${LABEL_UTILITIES.join('-, ')}- prints it as glyphs: the token on a `
      + 'border, a ring, an underline or a caret is a hairline nobody reads a word off. '
      + 'If the label is real but lives on a child element, move it into the same class string. '
      + 'If it is real but gated behind a state the fill is not, the resting element is still unlabeled '
      + '-- give the label the same variant prefix or drop the prefix, and for a JS condition '
      + '(`active ? ... : ...`, `active && ...`) merge the label into the fill\'s own string. '
      + 'If a mark is deliberately exempt, pin it in ACCEPTED_BARE_FILLS with its reason.',
    );
  }

  for (const [key, { reason, marks }] of ACCEPTED_BARE_FILLS) {
    const actual = (found.get(key) ?? []).map(({ signature }) => signature).sort();
    const pinned = [...marks].sort();
    if (actual.length !== pinned.length || pinned.some((signature, index) => signature !== actual[index])) {
      throw new Error(
        `ACCEPTED_BARE_FILLS pins ${key} (${reason}) to:\n`
        + pinned.map((signature) => `    ${signature}`).join('\n')
        + '\n  but src/ now carries:\n'
        + (actual.length > 0 ? actual.map((signature) => `    ${signature}`).join('\n') : '    (nothing)')
        + '\n  An exemption covers the construct it was granted for, not whatever later appears under the '
        + 'same file and accent -- re-pin it here once the change is intended.',
      );
    }
  }
}

// The scan above reads Tailwind utility NAMES, so it only sees an accent that arrives as
// one. `var(--mint)` written into a TypeScript string does not: it reaches a pixel through
// a `style={{ stroke }}` object, a `shadow-[inset_0_2px_0_0_var(--cyan)]` arbitrary value,
// or a lookup table read two files away from the element it paints. All three shipped --
// the agent graph drew 1.6px `var(--mint)` spawn edges, two file-extension icon maps
// tinted their glyphs through `currentColor`, and the editor and terminal tab strips
// underlined the active tab -- and the utility scan saw none of them.
//
// So this fence is the class of carrier rather than another utility name: no TypeScript
// string literal may name a bare accent. One rule closes the style object, the JSX paint
// attribute, the arbitrary value and the lookup table together, and it closes the carrier
// nobody has invented yet, because what it constrains is the string and not the place the
// string is used. `var(--mint-ink)` and `var(--mint-foreground)` are unaffected -- only
// the bare token is a mark with no reading under which it was intended.
//
// No pin map, deliberately. The remedy is never an exemption: a mark takes the -ink
// token, and a fill that genuinely carries a label belongs in a class string where the
// pairing assertion above can read it -- an inline `style` is exactly where such a
// pairing goes to hide. Zero sites need an exception after this change, so a pin map
// would be speculative; add one when a real site earns it.
function assertNoBareAccentVarInSource(root) {
  const bareVar = new RegExp(BARE_ACCENT_VAR.source, 'g');
  const marks = [];

  for (const file of sourceFiles(root)) {
    for (const { text, owner, line } of classStrings(file, fs.readFileSync(file, 'utf8'))) {
      for (const [, accent] of text.matchAll(bareVar)) {
        marks.push({ file, line, accent, signature: markSignature(owner, text) });
      }
    }
  }

  if (marks.length > 0) {
    throw new Error(
      'These name a bare accent inside a TypeScript string, so it reaches a pixel without ever '
      + 'being a Tailwind utility -- through an inline style, an arbitrary value, or a lookup table '
      + 'read somewhere else -- and the class scan cannot see it. Use the ink token:\n'
      + marks.map(({ file, line, accent, signature }) => (
        `  ${file}:${line} var(--${accent}) -> var(--${accent}-ink)\n    ${signature}`
      )).join('\n')
      + '\nIf it is genuinely a labelled fill, move the paint into a class string '
      + '(bg-mint text-mint-foreground) where the pairing assertion can read the label.',
    );
  }
}

assertAccentSpellingsAreCovered();
assertUnlabeledFillsTakeTheInk('src');
assertNoBareAccentVarInSource('src');
assertEveryAcceptedPairStillExists();

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
