import { beforeAll, describe, expect, it } from 'vitest';
import { ESLint } from 'eslint';

import sharedConfig from '../eslint.config.js';

// Probes the real `eslint.config.js` rather than a stand-in, so an accidental
// loosening of the shared config fails here instead of quietly widening what
// the lint gate tolerates across the whole UI.
let eslint;

beforeAll(() => {
  eslint = new ESLint();
});

async function messagesFor(code, filePath = 'src/__probe__/probe.ts') {
  const [result] = await eslint.lintText(code, { filePath });
  return result.messages;
}

// Presence is not the property these probes defend. `npm run lint` tallies
// severity-2 messages only, so a rule quietly demoted to a warning keeps showing
// up in `messages` while no longer being enforced anywhere: the probe would still
// pass, the baseline would not grow, and the class would retire in silence.
// Every "still reports" case therefore asserts that the rule *errors*.
async function errorRuleIdsFor(code, filePath) {
  return (await messagesFor(code, filePath))
    .filter((message) => message.severity === 2)
    .map((message) => message.ruleId);
}

// The allowed-shape cases stay severity-blind on purpose — that is the stricter
// assertion. A rule that starts merely warning about a shape the convention
// permits is still noise this config exists to prevent.
async function ruleIdsFor(code, filePath) {
  return (await messagesFor(code, filePath)).map((message) => message.ruleId);
}

// A behavioural probe only defends the rules someone thought to write a probe
// for. This one defends all of them at once. `npm run lint` tallies severity-2
// messages, so demoting a rule to a warning — or switching it off — retires it
// from the gate without changing a single count in `eslint-baseline.json`.
// Pinning the resolved severity of the whole config makes that edit loud, for
// `react-hooks/refs`, for a rule this PR emptied such as `react-hooks/globals`,
// and for rules that do not exist yet.
const WARNING_RULES = {
  'react-hooks/exhaustive-deps': 'Deliberate: the 54 remaining warnings are ledgered, not fixed.',
  'react-hooks/incompatible-library': 'Upstream advisory about libraries the compiler cannot analyse.',
  'react-hooks/unsupported-syntax': 'Upstream advisory about syntax the compiler cannot analyse.',
};

// Base ESLint rules that `typescript-eslint`'s recommended preset switches off
// because a type-aware equivalent supersedes them. A dependency bump that
// changes this set is *meant* to fail here: the diff is the review prompt.
const SUPERSEDED_BASE_RULES = [
  'constructor-super',
  'getter-return',
  'no-array-constructor',
  'no-class-assign',
  'no-const-assign',
  'no-dupe-args',
  'no-dupe-class-members',
  'no-dupe-keys',
  'no-func-assign',
  'no-import-assign',
  'no-new-native-nonconstructor',
  'no-new-symbol',
  'no-obj-calls',
  'no-redeclare',
  'no-setter-return',
  'no-this-before-super',
  'no-undef',
  'no-unreachable',
  'no-unsafe-negation',
  'no-unused-expressions',
  'no-unused-vars',
  'no-with',
];

const LEVELS = { off: 0, warn: 1, error: 2 };

async function severityByRule(filePath = 'src/__probe__/Probe.tsx') {
  const { rules = {} } = await eslint.calculateConfigForFile(filePath);
  return Object.fromEntries(
    Object.entries(rules).map(([id, entry]) => {
      const level = Array.isArray(entry) ? entry[0] : entry;
      return [id, LEVELS[level] ?? level];
    }),
  );
}

const rulesAt = (severities, level) =>
  Object.entries(severities)
    .filter(([, severity]) => severity === level)
    .map(([id]) => id)
    .sort();

describe('the shared config keeps every rule at the severity the gate counts', () => {
  it('leaves no rule warning except the recorded ones', async () => {
    expect(rulesAt(await severityByRule(), 1)).toEqual(Object.keys(WARNING_RULES).sort());
  });

  it('turns off only the base rules typescript-eslint supersedes', async () => {
    expect(rulesAt(await severityByRule(), 0)).toEqual([...SUPERSEDED_BASE_RULES].sort());
  });

  it('keeps the same severities for a plain .ts file', async () => {
    expect(await severityByRule('src/__probe__/probe.ts')).toEqual(await severityByRule());
  });
});

// The block above pins how loudly each rule fires. It says nothing about which
// files are looked at, and `eslint-baseline.json` records only what was found —
// never what was scanned. So widening `globalIgnores` exempts arbitrary source
// with no signal anywhere: the pairs that file used to contribute simply go
// stale, and the next `npm run lint:baseline` writes the exemption in as if the
// debt had been paid. These probes are the other half of the chokepoint. They
// pin the scope itself at its owner, `eslint.config.js`.
//
// Three outcomes are possible for a path, and the vocabulary matters because the
// PR body and the plan use it too:
//   linted      - not ignored, and the TypeScript rule set resolves for it
//   parsed only - not ignored, but no rule resolves; only fatal parse/config
//                 errors can ever be reported, which `fatalProblems` catches
//   ignored     - ESLint never opens it
async function scopeOf(filePath) {
  if (await eslint.isPathIgnored(filePath)) return 'ignored';
  const { rules = {} } = await eslint.calculateConfigForFile(filePath);
  return Object.keys(rules).length > 0 ? 'linted' : 'parsed only';
}

const globalIgnoresOf = (config) =>
  config.filter((entry) => entry.ignores && !entry.files).flatMap((entry) => entry.ignores);

const ruleBearing = (config) =>
  config.filter((entry) => entry.rules && Object.keys(entry.rules).length > 0);

// `extends` expands into one config object per preset, and typescript-eslint's
// `eslint-recommended` uses the nested form where the inner arrays are
// AND-combined. Flatten before comparing: the property under test is which
// extensions can be reached at all, not how the presets nest.
const scopePatternsOf = (config) =>
  [...new Set(ruleBearing(config).flatMap((entry) => (entry.files ?? []).flat()))].sort();

describe('the shared config keeps the lint scope the ledger assumes', () => {
  it('ignores nothing but the build output', () => {
    expect(globalIgnoresOf(sharedConfig)).toEqual(['dist']);
  });

  it('confines every rule to TypeScript sources', () => {
    expect(scopePatternsOf(sharedConfig)).toEqual([
      '**/*.cts',
      '**/*.mts',
      '**/*.ts',
      '**/*.tsx',
      '**/*.{ts,tsx}',
    ]);
  });

  // A rule-bearing config with no `files` applies everywhere, so its scope would
  // be invisible to the union above rather than wrong in it.
  it('leaves no rule-bearing config without an explicit scope', () => {
    expect(ruleBearing(sharedConfig).filter((entry) => !entry.files?.length)).toEqual([]);
  });
});

describe('a source file nobody has written yet is still measured', () => {
  it('lints an arbitrary new .ts file', async () => {
    expect(await scopeOf('src/does/not/exist/yet/newModule.ts')).toBe('linted');
  });

  it('lints an arbitrary new .tsx file', async () => {
    expect(await scopeOf('src/does/not/exist/yet/NewComponent.tsx')).toBe('linted');
  });

  // Resolving rules for a path is not the same as reporting on it. This asserts
  // the whole way through to a severity-2 message, which is what the gate counts.
  it('errors on new debt in that unwritten file', async () => {
    expect(
      await errorRuleIdsFor('export const f = (v: any) => v;\n', 'src/does/not/exist/yet/newModule.ts'),
    ).toContain('@typescript-eslint/no-explicit-any');
  });

  it('keeps the build output out of the gate', async () => {
    expect(await scopeOf('dist/assets/index-abcdef12.js')).toBe('ignored');
  });

  // Recorded, not aspirational: `npm run lint` opens the gate script and the
  // config itself, but no rule applies to either, so the only thing that can be
  // reported for them is a fatal parse/config error. Broadening the scope to
  // JS/MJS is a separate decision; this test makes it a visible one.
  it('parses the gate script and the config without applying the rule set', async () => {
    expect(await scopeOf('scripts/lint-baseline.mjs')).toBe('parsed only');
    expect(await scopeOf('eslint.config.js')).toBe('parsed only');
  });
});

const UNUSED = '@typescript-eslint/no-unused-vars';

describe('no-unused-vars honours the `_` opt-out convention', () => {
  it('allows an intentionally unused function parameter', async () => {
    expect(await ruleIdsFor('export const f = (_value: string) => 1;\n')).not.toContain(UNUSED);
  });

  it('allows an intentionally unused local binding', async () => {
    expect(await ruleIdsFor('export function f() {\n  const _legacy = 1;\n  return 2;\n}\n')).not.toContain(UNUSED);
  });

  it('allows an intentionally unused caught error', async () => {
    expect(await ruleIdsFor('export function f() {\n  try {\n    JSON.parse("1");\n  } catch (_err) {\n    return null;\n  }\n  return 1;\n}\n')).not.toContain(UNUSED);
  });

  it('allows an intentionally unused destructured array slot', async () => {
    expect(await ruleIdsFor('export function f(pair: [number, number]) {\n  const [_drop, keep] = pair;\n  return keep;\n}\n')).not.toContain(UNUSED);
  });
});

describe('no-unused-vars still errors on genuinely dead bindings', () => {
  it('errors on an unused function parameter that is not opted out', async () => {
    expect(await errorRuleIdsFor('export const f = (value: string) => 1;\n')).toContain(UNUSED);
  });

  it('errors on an unused local binding that is not opted out', async () => {
    expect(await errorRuleIdsFor('export function f() {\n  const legacy = 1;\n  return 2;\n}\n')).toContain(UNUSED);
  });

  it('errors on an unused caught error that is not opted out', async () => {
    expect(await errorRuleIdsFor('export function f() {\n  try {\n    JSON.parse("1");\n  } catch (err) {\n    return null;\n  }\n  return 1;\n}\n')).toContain(UNUSED);
  });
});

// `src/lib/useLatestRef.ts` carries the only `react-hooks/refs` exemption that is
// not a per-site judgement call. That is defensible only while the rule still
// applies everywhere else: switching it off in the shared config, or dropping it
// to a warning, would silently retire the whole class. Pin both directions.
const REFS = 'react-hooks/refs';
const componentUsing = (body) =>
  `import { useRef } from 'react';\nexport function C({ v }: { v: number }) {\n  const r = useRef(v);\n${body}\n}\n`;

describe('react-hooks/refs still errors on the ordinary ref misuse', () => {
  it('errors on a ref read during render', async () => {
    expect(
      await errorRuleIdsFor(componentUsing('  return <div>{r.current}</div>;'), 'src/__probe__/Probe.tsx'),
    ).toContain(REFS);
  });

  it('errors on a ref write during render', async () => {
    expect(
      await errorRuleIdsFor(componentUsing('  r.current = v;\n  return <div>ok</div>;'), 'src/__probe__/Probe.tsx'),
    ).toContain(REFS);
  });

  it('leaves a ref touched only from an effect alone', async () => {
    const code =
      "import { useEffect, useRef } from 'react';\n" +
      'export function C({ v }: { v: number }) {\n' +
      '  const r = useRef(v);\n' +
      '  useEffect(() => {\n    r.current = v;\n  }, [v]);\n' +
      '  return <div>ok</div>;\n}\n';
    expect(await ruleIdsFor(code, 'src/__probe__/Probe.tsx')).not.toContain(REFS);
  });
});
