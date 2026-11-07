// The lab's settings, kept in the browser's own storage (main.tsx imports this first, before the design
// shell swaps Web Storage for an in-memory one), and how each takes effect on the page:
// - variant: `data-disclosure-variant` on the root (src/lib/transcriptMotion, src/index.css);
// - theme: `data-theme` on the root, as the app's ThemeProvider sets it;
// - reduced motion: the page answers `prefers-reduced-motion` as the switch says, in its stylesheets and
//   to `matchMedia`, so the product's own reduced forms run;
// - slow motion: every motion duration token at 4x, so CSS and scripted motion slow down together.

export type Variant = 'a' | 'b' | 'c' | 'd';
export type Speed = '1' | '0.75' | '0.5';
export type LabPrefs = { variant: Variant; speed: Speed; theme: 'light' | 'dark'; reduced: boolean; slow: boolean; open: boolean };

const KEY = 'avibe-lab-disclosure-v2';
const storage: Storage | null = (() => {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
})();

const systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
const defaults: LabPrefs = { variant: 'd', speed: '0.5', theme: systemDark ? 'dark' : 'light', reduced: false, slow: false, open: true };

export function loadPrefs(): LabPrefs {
  try {
    const saved = JSON.parse(storage?.getItem(KEY) ?? '{}') as Partial<LabPrefs>;
    return { ...defaults, ...saved };
  } catch {
    return defaults;
  }
}

export function savePrefs(prefs: LabPrefs) {
  try {
    storage?.setItem(KEY, JSON.stringify(prefs));
  } catch {
    // Private mode: the switch still works for this visit.
  }
}

// ----- Reduced motion, as the page answers it -----

let reduced = loadPrefs().reduced;
type Listener = (event: { matches: boolean; media: string }) => void;
const listeners = new Set<{ query: string; fn: Listener }>();
const nativeMatchMedia = window.matchMedia.bind(window);
const answers = (query: string) => (/no-preference/.test(query) ? !reduced : reduced);

window.matchMedia = (query: string): MediaQueryList => {
  if (!/prefers-reduced-motion/.test(query)) return nativeMatchMedia(query);
  const own = new Map<Listener, Listener>();
  const list = {
    media: query,
    get matches() {
      return answers(query);
    },
    onchange: null as ((event: MediaQueryListEvent) => void) | null,
    addListener(fn: Listener) {
      const entry = { query, fn };
      listeners.add(entry);
      own.set(fn, fn);
    },
    removeListener(fn: Listener) {
      for (const entry of listeners) if (entry.fn === fn) listeners.delete(entry);
    },
    addEventListener(_type: string, fn: Listener) {
      list.addListener(fn);
    },
    removeEventListener(_type: string, fn: Listener) {
      list.removeListener(fn);
    },
    dispatchEvent: () => true,
  };
  return list as unknown as MediaQueryList;
};

const originalMedia = new WeakMap<CSSMediaRule, string>();
function rewrite(rules: CSSRuleList) {
  for (const rule of Array.from(rules)) {
    if (rule instanceof CSSMediaRule) {
      const original = originalMedia.get(rule) ?? rule.media.mediaText;
      originalMedia.set(rule, original);
      if (/prefers-reduced-motion/.test(original)) {
        rule.media.mediaText = reduced
          ? original.replace(/\(prefers-reduced-motion:\s*reduce\)/g, '(min-width: 0px)').replace(/\(prefers-reduced-motion:\s*no-preference\)/g, '(max-width: 0px)')
          : original;
      }
    }
    if ('cssRules' in rule) rewrite((rule as CSSGroupingRule).cssRules);
  }
}

/** Every stylesheet's `prefers-reduced-motion` rules, as the switch says. */
export function applyReducedStyles() {
  for (const sheet of Array.from(document.styleSheets)) {
    try {
      rewrite(sheet.cssRules);
    } catch {
      // A sheet from another origin: none of ours.
    }
  }
}

export function setReduced(value: boolean) {
  reduced = value;
  applyReducedStyles();
  for (const { query, fn } of listeners) fn({ matches: answers(query), media: query });
}

// ----- Slow motion -----

const DURATION_TOKENS = [
  '--motion-duration-instant',
  '--motion-duration-fast',
  '--motion-duration-base',
  '--motion-duration-slow',
  '--motion-stagger',
  '--motion-spring-snappy-duration',
  '--motion-spring-smooth-duration',
  '--motion-spring-sheet-duration',
  '--motion-nav-push-duration',
  '--motion-nav-pop-duration',
  '--motion-handover-duration',
  '--motion-shimmer-duration',
  '--motion-beat',
  '--motion-press-delay',
  '--motion-disclose-open-duration',
  '--motion-disclose-close-duration',
  '--motion-disclose-summary-duration',
];
export const SLOW_FACTOR = 4;

function ms(value: string): number | null {
  const amount = parseFloat(value);
  if (!Number.isFinite(amount)) return null;
  return value.trim().endsWith('ms') ? amount : value.trim().endsWith('s') ? amount * 1000 : amount;
}

/** Every motion duration at its own value, or at `SLOW_FACTOR` times it. */
export function applySlow(slow: boolean) {
  const root = document.documentElement;
  for (const name of DURATION_TOKENS) root.style.removeProperty(name);
  if (!slow) return;
  const style = getComputedStyle(root);
  const values = DURATION_TOKENS.map((name) => [name, ms(style.getPropertyValue(name))] as const);
  for (const [name, value] of values) if (value !== null) root.style.setProperty(name, `${value * SLOW_FACTOR}ms`);
}

export function applyTheme(theme: 'light' | 'dark') {
  const root = document.documentElement;
  root.setAttribute('data-theme', theme);
  root.style.colorScheme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'dark' ? '#0b0d12' : '#ffffff');
}

export function applyVariant(variant: Variant, speed: Speed) {
  document.documentElement.dataset.disclosureVariant = variant;
  document.documentElement.dataset.disclosureSpeed = speed;
}

/** Everything the saved settings say, on the page. */
export function applyAll(prefs: LabPrefs) {
  applyTheme(prefs.theme);
  applyVariant(prefs.variant, prefs.speed);
  setReduced(prefs.reduced);
  applySlow(prefs.slow);
}
