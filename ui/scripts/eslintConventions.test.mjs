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

const TSX_PROBE = 'src/__probe__/Probe.tsx';
const TS_PROBE = 'src/__probe__/probe.ts';

async function messagesFor(code, filePath = TS_PROBE) {
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

// ─── the canonical enforcement contract ──────────────────────────────────────
//
// Two whole subjects, compared for equality. Nothing here is a filtered view of
// one, and that is the point. Earlier revisions pinned this policy with
// projections — the warning ids, the disabled ids, a union of scope patterns,
// a count of resolved rules — and every projection has a blind complement:
//
//   * asserting levels 0 and 1 says nothing about a level-2 rule that disappears
//     outright. An upstream preset that drops a rule with no current violations
//     retires it from the gate with no baseline entry going stale, no message
//     anywhere, and `npm run lint` still printing "no drift".
//   * asserting global ignores says nothing about `ignores` on a rule-bearing
//     entry, which exempts a subtree while the union of `files` is unchanged.
//   * asserting that *some* rules resolve says nothing about *which*: a path can
//     resolve one rule out of 106 and still read as fully linted.
//
// So: pin the whole severity map, pin every config entry's scope, and derive
// every other classification from those two. A dependency upgrade is meant to
// fail here — the diff is the review prompt, and it is the only place a change
// in enforcement policy is visible before it silently changes what
// `eslint-baseline.json` is allowed to record.
//
// Level 0 is not "we switched it off". It is a base ESLint rule that
// `typescript-eslint`'s recommended preset disables because a type-aware
// equivalent supersedes it.
const EXPECTED_SEVERITY_MAP = {
  '@typescript-eslint/ban-ts-comment': 2,
  '@typescript-eslint/no-array-constructor': 2,
  '@typescript-eslint/no-duplicate-enum-values': 2,
  '@typescript-eslint/no-empty-object-type': 2,
  '@typescript-eslint/no-explicit-any': 2,
  '@typescript-eslint/no-extra-non-null-assertion': 2,
  '@typescript-eslint/no-misused-new': 2,
  '@typescript-eslint/no-namespace': 2,
  '@typescript-eslint/no-non-null-asserted-optional-chain': 2,
  '@typescript-eslint/no-require-imports': 2,
  '@typescript-eslint/no-this-alias': 2,
  '@typescript-eslint/no-unnecessary-type-constraint': 2,
  '@typescript-eslint/no-unsafe-declaration-merging': 2,
  '@typescript-eslint/no-unsafe-function-type': 2,
  '@typescript-eslint/no-unused-expressions': 2,
  '@typescript-eslint/no-unused-vars': 2,
  '@typescript-eslint/no-wrapper-object-types': 2,
  '@typescript-eslint/prefer-as-const': 2,
  '@typescript-eslint/prefer-namespace-keyword': 2,
  '@typescript-eslint/triple-slash-reference': 2,
  'constructor-super': 0,
  'for-direction': 2,
  'getter-return': 0,
  'no-array-constructor': 0,
  'no-async-promise-executor': 2,
  'no-case-declarations': 2,
  'no-class-assign': 0,
  'no-compare-neg-zero': 2,
  'no-cond-assign': 2,
  'no-const-assign': 0,
  'no-constant-binary-expression': 2,
  'no-constant-condition': 2,
  'no-control-regex': 2,
  'no-debugger': 2,
  'no-delete-var': 2,
  'no-dupe-args': 0,
  'no-dupe-class-members': 0,
  'no-dupe-else-if': 2,
  'no-dupe-keys': 0,
  'no-duplicate-case': 2,
  'no-empty': 2,
  'no-empty-character-class': 2,
  'no-empty-pattern': 2,
  'no-empty-static-block': 2,
  'no-ex-assign': 2,
  'no-extra-boolean-cast': 2,
  'no-fallthrough': 2,
  'no-func-assign': 0,
  'no-global-assign': 2,
  'no-import-assign': 0,
  'no-invalid-regexp': 2,
  'no-irregular-whitespace': 2,
  'no-loss-of-precision': 2,
  'no-misleading-character-class': 2,
  'no-new-native-nonconstructor': 0,
  'no-new-symbol': 0,
  'no-nonoctal-decimal-escape': 2,
  'no-obj-calls': 0,
  'no-octal': 2,
  'no-prototype-builtins': 2,
  'no-redeclare': 0,
  'no-regex-spaces': 2,
  'no-self-assign': 2,
  'no-setter-return': 0,
  'no-shadow-restricted-names': 2,
  'no-sparse-arrays': 2,
  'no-this-before-super': 0,
  'no-undef': 0,
  'no-unexpected-multiline': 2,
  'no-unreachable': 0,
  'no-unsafe-finally': 2,
  'no-unsafe-negation': 0,
  'no-unsafe-optional-chaining': 2,
  'no-unused-expressions': 0,
  'no-unused-labels': 2,
  'no-unused-private-class-members': 2,
  'no-unused-vars': 0,
  'no-useless-backreference': 2,
  'no-useless-catch': 2,
  'no-useless-escape': 2,
  'no-var': 2,
  'no-with': 0,
  'prefer-const': 2,
  'prefer-rest-params': 2,
  'prefer-spread': 2,
  'react-hooks/component-hook-factories': 2,
  'react-hooks/config': 2,
  'react-hooks/error-boundaries': 2,
  'react-hooks/exhaustive-deps': 1, // deliberate: the 54 remaining warnings are ledgered, not fixed
  'react-hooks/gating': 2,
  'react-hooks/globals': 2,
  'react-hooks/immutability': 2,
  'react-hooks/incompatible-library': 1, // deliberate: upstream advisory about libraries the compiler cannot analyse
  'react-hooks/preserve-manual-memoization': 2,
  'react-hooks/purity': 2,
  'react-hooks/refs': 2,
  'react-hooks/rules-of-hooks': 2,
  'react-hooks/set-state-in-effect': 2,
  'react-hooks/set-state-in-render': 2,
  'react-hooks/static-components': 2,
  'react-hooks/unsupported-syntax': 1, // deliberate: upstream advisory about syntax the compiler cannot analyse
  'react-hooks/use-memo': 2,
  'react-refresh/only-export-components': 2,
  'require-yield': 2,
  'use-isnan': 2,
  'valid-typeof': 2,
};

// `eslint-baseline.json` records what was *found*, never what was *looked at*, so
// narrowing the scope pays debt down on paper. Keeping each entry's `files` and
// `ignores` together, in cascade order, is what makes the pair inseparable — a
// scoped ignore cannot hide behind an unchanged `files`. Nested `files` arrays
// are AND-combined by `typescript-eslint/eslint-recommended`; the shape is kept
// rather than flattened, because flattening is itself a projection. Entries are
// listed for the whole config, so a rule-bearing entry added with no scope at all
// shows up as a new `files: null` row instead of vanishing from a union.
const EXPECTED_SCOPE_CONTRACT = [
  { name: 'globalIgnores 0', files: null, ignores: ['dist'] },
  { name: 'UserConfig[0][1] > ExtendedConfig[0]', files: ['**/*.{ts,tsx}'], ignores: null }, // js.configs.recommended
  { name: 'UserConfig[0][1] > typescript-eslint/base', files: ['**/*.{ts,tsx}'], ignores: null },
  {
    name: 'UserConfig[0][1] > typescript-eslint/eslint-recommended',
    files: [
      ['**/*.{ts,tsx}', '**/*.ts'],
      ['**/*.{ts,tsx}', '**/*.tsx'],
      ['**/*.{ts,tsx}', '**/*.mts'],
      ['**/*.{ts,tsx}', '**/*.cts'],
    ],
    ignores: null,
  },
  { name: 'UserConfig[0][1] > typescript-eslint/recommended', files: ['**/*.{ts,tsx}'], ignores: null },
  { name: 'UserConfig[0][1] > ExtendedConfig[2]', files: ['**/*.{ts,tsx}'], ignores: null }, // reactHooks.configs.flat.recommended
  { name: 'UserConfig[0][1] > react-refresh/vite', files: ['**/*.{ts,tsx}'], ignores: null },
  { name: null, files: ['**/*.{ts,tsx}'], ignores: null }, // the repo's own rule overrides
];

const LEVELS = { off: 0, warn: 1, error: 2 };

async function severityByRule(filePath = TSX_PROBE) {
  const config = await eslint.calculateConfigForFile(filePath);
  return Object.fromEntries(
    Object.entries(config?.rules ?? {}).map(([id, entry]) => {
      const level = Array.isArray(entry) ? entry[0] : entry;
      return [id, LEVELS[level] ?? level];
    }),
  );
}

const scopeContractOf = (config) =>
  config.map((entry) => ({
    name: entry.name ?? null,
    files: entry.files ?? null,
    ignores: entry.ignores ?? null,
  }));

const canonical = (severities) =>
  JSON.stringify(Object.entries(severities).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)));

// Three outcomes are intended for a path, and the vocabulary matters because the
// PR body and the plan use it too:
//   linted      - not ignored, and the config resolves exactly EXPECTED_SEVERITY_MAP
//   parsed only - not ignored, and the config resolves the empty rule map; only a
//                 fatal parse/config error can be reported, which `fatalProblems`
//                 catches
//   ignored     - ESLint never opens it
// Anything else is a fourth thing and says so. Classifying by rule *count* would
// fold a path that resolves one rule into `linted`, which is the same blindness
// this contract exists to remove.
async function scopeOf(filePath) {
  if (await eslint.isPathIgnored(filePath)) return 'ignored';
  const severities = await severityByRule(filePath);
  const resolved = Object.keys(severities).length;
  if (resolved === 0) return 'parsed only';
  if (canonical(severities) === canonical(EXPECTED_SEVERITY_MAP)) return 'linted';
  const pinned = Object.keys(EXPECTED_SEVERITY_MAP).length;
  return `not the pinned rule set (resolved ${resolved}, pinned ${pinned})`;
}

describe('the shared config resolves exactly the policy the gate counts', () => {
  it('pins every resolved rule and its severity for a .tsx source', async () => {
    expect(await severityByRule(TSX_PROBE)).toEqual(EXPECTED_SEVERITY_MAP);
  });

  it('pins the same policy for a plain .ts source', async () => {
    expect(await severityByRule(TS_PROBE)).toEqual(EXPECTED_SEVERITY_MAP);
  });
});

describe('the shared config keeps the lint scope the ledger assumes', () => {
  it('pins the files and ignores of every config entry', () => {
    expect(scopeContractOf(sharedConfig)).toEqual(EXPECTED_SCOPE_CONTRACT);
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
  // config itself, but the rule map they resolve is exactly empty, so the only
  // thing that can be reported for them is a fatal parse/config error.
  // Broadening the scope to JS/MJS is a separate decision; this makes it visible.
  it('parses the gate script and the config without applying the rule set', async () => {
    expect(await scopeOf('scripts/lint-baseline.mjs')).toBe('parsed only');
    expect(await scopeOf('eslint.config.js')).toBe('parsed only');
    expect(await severityByRule('scripts/lint-baseline.mjs')).toEqual({});
    expect(await severityByRule('eslint.config.js')).toEqual({});
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
