import fs from 'node:fs';
import vm from 'node:vm';
import postcss from 'postcss';

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

function assertContrast(name, tokens, inkProp, surfaceProp) {
  const ink = requireMeasurableColor(name, inkProp, tokens.get(inkProp));
  const surface = requireMeasurableColor(name, surfaceProp, tokens.get(surfaceProp));

  const ratio = contrastRatio(ink, surface);
  if (ratio < AA_SMALL_TEXT) {
    throw new Error(
      `${name}: ${inkProp} on ${surfaceProp} is ${ratio.toFixed(2)}:1, below WCAG AA ${AA_SMALL_TEXT}:1`,
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

for (const [theme, tokens] of [['light', systemLight], ['dark', systemDark]]) {
  for (const fill of ACCENT_FILLS) {
    assertEqual(`${theme} ${fill} is defined`, tokens.has(fill), true);
    assertEqual(`${theme} ${fill} declares an ink`, tokens.has(`${fill}-ink`), true);
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
