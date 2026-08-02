import { beforeAll, describe, expect, it } from 'vitest';
import { ESLint } from 'eslint';

import { effectivePolicyOf, policyDifferences } from './lintPolicy.mjs';

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

// ─── what this file is for ───────────────────────────────────────────────────
//
// The enforcement policy itself, and the set of files it applies to, are pinned
// in `scripts/lintPolicy.mjs` and measured at runtime by `npm run lint`: every
// repo-owned .ts/.tsx file, found by walking the tree, must resolve the full
// expected rule configuration. That is a total statement, so it does not belong
// here as a second hand-written copy — earlier revisions kept one (a severity
// map, then a per-entry `{name, files, ignores}` contract) and each was a
// projection with a blind complement, most recently `basePath`.
//
// What stays here is the consuming evidence: resolving a rule for a path is not
// the same as reporting on it, so these probes drive the real config end to end
// and assert the messages the cleanup's conventions depend on.

const rulesFor = async (filePath) => (await eslint.calculateConfigForFile(filePath))?.rules ?? {};

const policyDriftFor = async (filePath) =>
  policyDifferences(effectivePolicyOf(await eslint.calculateConfigForFile(filePath)));

// The runtime invariant can only measure files that exist. These two paths do
// not, which is the one thing it cannot cover: whoever adds the next module
// inherits the policy without anyone re-running anything.
describe('a source file nobody has written yet is already covered', () => {
  it('resolves the pinned policy for an arbitrary new .ts file', async () => {
    expect(await policyDriftFor('src/does/not/exist/yet/newModule.ts')).toEqual([]);
  });

  it('resolves the pinned policy for an arbitrary new .tsx file', async () => {
    expect(await policyDriftFor('src/does/not/exist/yet/NewComponent.tsx')).toEqual([]);
  });

  // Resolving rules for a path is not the same as reporting on it. This asserts
  // the whole way through to a severity-2 message, which is what the gate counts.
  it('errors on new debt in that unwritten file', async () => {
    expect(
      await errorRuleIdsFor('export const f = (v: any) => v;\n', 'src/does/not/exist/yet/newModule.ts'),
    ).toContain('@typescript-eslint/no-explicit-any');
  });

  it('keeps the build output out of the gate', async () => {
    expect(await eslint.isPathIgnored('dist/assets/index-abcdef12.js')).toBe(true);
  });

  // The documented boundary of the TypeScript-only policy. `npm run lint` opens
  // the gate script and the config itself, but the rule map they resolve is
  // exactly empty, so the only thing reportable for them is a fatal parse or
  // config error. Broadening the scope to JS/MJS is a separate decision; this
  // is where it would become visible.
  it('parses the gate script and the config without applying the rule set', async () => {
    expect(await rulesFor('scripts/lint-baseline.mjs')).toEqual({});
    expect(await rulesFor('eslint.config.js')).toEqual({});
    expect(await eslint.isPathIgnored('scripts/lint-baseline.mjs')).toBe(false);
    expect(await eslint.isPathIgnored('eslint.config.js')).toBe(false);
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
      await errorRuleIdsFor(componentUsing('  return <div>{r.current}</div>;'), TSX_PROBE),
    ).toContain(REFS);
  });

  it('errors on a ref write during render', async () => {
    expect(
      await errorRuleIdsFor(componentUsing('  r.current = v;\n  return <div>ok</div>;'), TSX_PROBE),
    ).toContain(REFS);
  });

  it('leaves a ref touched only from an effect alone', async () => {
    const code =
      "import { useEffect, useRef } from 'react';\n" +
      'export function C({ v }: { v: number }) {\n' +
      '  const r = useRef(v);\n' +
      '  useEffect(() => {\n    r.current = v;\n  }, [v]);\n' +
      '  return <div>ok</div>;\n}\n';
    expect(await ruleIdsFor(code, TSX_PROBE)).not.toContain(REFS);
  });
});
