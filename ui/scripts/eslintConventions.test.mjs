import { beforeAll, describe, expect, it } from 'vitest';
import { ESLint } from 'eslint';

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
