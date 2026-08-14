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
// no label and so must take --X-ink. The two assertions below cover the two places
// that distinction gets lost: a CSS rule painting a wire, and a Tailwind utility
// painting a dot.
//
// Both are needed because neither the ink nor the fill assertion above can see this.
// They check that each token is legible against the surface it claims; they cannot
// know that .model-hub-wire--gateway chose the fill. That is how the supply graph
// shipped 2px --mint strokes at 2.35:1 on the light canvas with every token guard
// passing. Light is where it bites: --mint reads 2.35:1 on --background and --gold
// 2.95:1, both under the 3:1 non-text floor, while their inks clear it. In dark each
// accent's ink IS its fill, so none of this moves a dark pixel.

// A translucent value is a wash, not a mark -- it has no ratio of its own, exactly as
// requireMeasurableColor says -- so only opaque declarations are held to the rule.
const ACCENT_NAMES = ACCENT_FILLS.map((fill) => fill.slice(2));
const BARE_ACCENT_VAR = new RegExp(`var\\(\\s*--(${ACCENT_NAMES.join('|')})\\s*\\)`);

// Naming color-mix() is not proof of translucency, and that is the whole exemption.
// `color-mix(in srgb, var(--mint) 100%, transparent)` mentions both `color-mix` and
// `transparent` and still renders as bare mint at 2.35:1 on the light canvas -- exactly
// the mark this assertion exists to reject. So resolve the accent's share and exempt
// only what a wash actually is.
//
// A mix shape this pattern cannot read fails loudly rather than inheriting the old
// free pass: an unmeasured mix is not a proven-translucent one. Extending the pattern
// is the correct response to that failure. Only the accent's own share is resolved --
// mixing an accent into an opaque colour keeps its ratio, so such a shape is a mark
// and must say so here rather than be waved through.
const ACCENT_SHARE_OF_MIX = /^color-mix\(\s*in\s+[\w-]+\s*,\s*[^,]+?\s+([\d.]+)%\s*,\s*transparent\s*\)$/i;

function accentShare(name, decl, value) {
  if (!value.includes('color-mix')) {
    return 1;
  }

  const mix = ACCENT_SHARE_OF_MIX.exec(value);
  if (!mix) {
    throw new Error(
      `${name}: ${decl.selector ?? decl.parent?.selector} paints ${decl.prop} with ${value}, a `
      + 'color-mix() this guard cannot resolve to an alpha. Only a mix proven translucent is exempt '
      + 'from the mark rule, because an opaque mix renders as the bare fill -- teach '
      + 'ACCENT_SHARE_OF_MIX this shape instead of letting an unmeasured mix through.',
    );
  }

  return Number.parseFloat(mix[1]) / 100;
}

function assertMarksTakeTheInk(source, name) {
  postcss.parse(source).walkDecls((decl) => {
    if (decl.prop !== 'stroke' && decl.prop !== 'fill') {
      return;
    }

    const value = normalizeCssValue(decl.value);
    if (!BARE_ACCENT_VAR.test(value) || accentShare(name, decl, value) < 1) {
      return;
    }

    const accent = BARE_ACCENT_VAR.exec(value)[1];
    throw new Error(
      `${name}: ${decl.selector ?? decl.parent?.selector} paints ${decl.prop} with var(--${accent}), the fill. `
      + 'A stroke or an SVG fill is a mark on the bare canvas -- no label is printed on it, so the '
      + `pairing that licenses a vivid fill does not apply. Use var(--${accent}-ink), which is the same `
      + 'value in dark and a legible one in light.',
    );
  });
}

assertMarksTakeTheInk(modelHubCss, 'model hub');

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

  const scopeOf = (node) => {
    const parent = node.parent;
    if (ts.isJsxAttribute(parent) || (ts.isJsxExpression(parent) && ts.isJsxAttribute(parent.parent))) {
      return ts.isJsxAttribute(parent) ? parent.parent : parent.parent.parent;
    }
    if (ts.isPropertyAssignment(parent) && ts.isObjectLiteralExpression(parent.parent)) {
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

  const record = (node, text) => {
    strings.push({
      text,
      owner: ownerOf(node),
      scope: scopeOf(node).getText(ast),
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

function assertUnlabeledFillsTakeTheInk(root) {
  const bare = new RegExp(`\\bbg-(${[...FILL_LABEL.keys(), 'pink'].join('|')})(?![\\w-/])`, 'g');
  const found = new Map();

  // readdirSync(recursive) yields platform separators, so on Windows an entry arrives
  // as `components\ui\switch.tsx`. Normalise before it becomes a lookup key, or every
  // pin below misses and validate:theme fails on an unchanged checkout.
  const files = fs.readdirSync(root, { recursive: true })
    .map((entry) => entry.split(path.sep).join('/'))
    .filter((entry) => /\.tsx?$/.test(entry))
    .map((entry) => `${root}/${entry}`)
    .sort();

  for (const file of files) {
    for (const { text, owner, scope, line } of classStrings(file, fs.readFileSync(file, 'utf8'))) {
      for (const match of text.matchAll(bare)) {
        const accent = match[1];
        const label = FILL_LABEL.get(accent);
        if (label && (text.includes(`-${label}`) || scope.includes(`-${label}`))) {
          continue;
        }

        const key = `${file} --${accent}`;
        found.set(key, [...found.get(key) ?? [], { signature: markSignature(owner, text), line }]);
      }
    }
  }

  const unpinned = [...found.entries()].filter(([key]) => !ACCEPTED_BARE_FILLS.has(key));
  if (unpinned.length > 0) {
    throw new Error(
      'These paint a bare accent fill with no paired *-foreground label in the same class string or on '
      + 'a sibling attribute/property, so they are marks read straight off the canvas and must use the '
      + '-ink token (bg-mint-ink, bg-gold-ink, ...):\n'
      + unpinned.map(([key, marks]) => marks.map(({ signature, line }) => `  ${key} at line ${line}\n    ${signature}`).join('\n')).join('\n')
      + '\nIf the label is real but lives on a child element, move it into the same class string. '
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

assertUnlabeledFillsTakeTheInk('src');
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
