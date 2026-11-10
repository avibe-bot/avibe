import fs from 'node:fs';
import { describe, expect, it } from 'vitest';
import { createLintTree, lintMessages, readConfig, writeSource } from './lintTestUtils.mjs';
import { measureLint } from './oxlintAdapter.mjs';
import { policyDifferences } from './lintPolicy.mjs';

// Exercise the configured Oxlint engine, not a stand-in for its native or JS
// rules. The separate policy tests pin every configuration boundary.

const TSX_PROBE = 'src/__probe__/Probe.tsx';
const TS_PROBE = 'src/__probe__/probe.ts';

async function messagesFor(code, filePath = TS_PROBE) {
  return lintMessages(code, filePath).messages;
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

describe('a source file nobody has written yet is already covered', () => {
  it.each(['src/does/not/exist/yet/newModule.ts', 'src/does/not/exist/yet/NewComponent.tsx', 'newRootConfig.ts'])(
    'errors on new debt in %s under the pinned policy', async (file) => {
      expect(policyDifferences(readConfig())).toEqual([]);
      expect(await errorRuleIdsFor('export const f = (v: any) => v;\n', file)).toContain('@typescript-eslint/no-explicit-any');
    },
  );

  it('keeps build output out and parses JS scripts without applying TS rules', () => {
    const root = createLintTree();
    try {
      writeSource(root, 'src/clean.ts', 'export const x = 1;');
      writeSource(root, 'dist/assets/bundle.ts', 'not valid TypeScript!');
      writeSource(root, 'tool.mjs', 'debugger;');
      let measured = measureLint(root);
      expect(measured.measurement.fatals).toEqual([]);
      expect(measured.measurement.intended).toEqual(['src/clean.ts']);
      expect(measured.results.flatMap((result) => result.messages)).toEqual([]);
      writeSource(root, 'tool.mjs', 'export const = ;');
      measured = measureLint(root);
      expect(measured.measurement.fatals).toEqual([expect.objectContaining({ filePath: 'tool.mjs' })]);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
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

// The dependency migration must preserve real enforcement, not just configured
// rule names. In particular Hooks 7.1 reports multiple compiler errors and sees
// synchronous effect setters that 7.0.1 missed. A snapshot alone cannot prove
// either behavior; these probes exercise the same config as the lint gate.
describe('the upgraded lint presets enforce their behavioral contract', () => {
  it.each([
    ['react-hooks/rules-of-hooks', 'import {useState} from "react"; export function C({ready}: {ready: boolean}) { if (ready) { const [n] = useState(0); return <div>{n}</div>; } return null; }'],
    ['react-hooks/static-components', 'export function Parent({value}: {value: string}) { function Child() { return <div>{value}</div>; } return <Child/>; }'],
    ['react-hooks/immutability', 'export function C({value}: {value: {name: string}}) { value.name="changed"; return <div>{value.name}</div>; }'],
    ['react-hooks/purity', 'export function C() { return <div>{Date.now()}</div>; }'],
    ['no-unassigned-vars', 'export function f() { let value; return value; }'],
    ['no-useless-assignment', 'export function f() { let n=1; n=2; return n; }'],
    // Native Oxlint misses these two existing try/finally patterns. The actual
    // configured JS rule must retain them; a native-only switch fails here.
    ['no-useless-assignment', 'export async function f(read) { let data = null; try { data = await read(); } finally { console.log("done"); } return data; }'],
    ['no-useless-assignment', 'export async function f(read) { let access = null; try { access = await read(); } catch { return null; } finally { console.log("done"); } return access; }'],
    ['preserve-caught-error', 'export function f() { try { JSON.parse("x"); } catch (error) { throw new Error(String(error)); } }'],
    ['no-shadow-restricted-names', 'export function f(globalThis: string) { return globalThis; }'],
  ])('errors on %s violations', async (rule, source) => {
    expect(await errorRuleIdsFor(source, TSX_PROBE)).toContain(rule);
  });

  it('reports both a synchronous effect setter and a render ref read, not just the first compiler error', async () => {
    const code =
      'import {useEffect,useRef,useState} from "react";\n' +
      'export function C({value}: {value: string}) {\n' +
      '  const ref=useRef(0); const [text,setText]=useState("");\n' +
      '  useEffect(()=>{setText(value);},[value]);\n' +
      '  return <div>{text}{ref.current}</div>;\n}\n';
    expect(await errorRuleIdsFor(code, TSX_PROBE)).toEqual(
      expect.arrayContaining(['react-hooks/set-state-in-effect', 'react-hooks/refs']),
    );
  });

  it.each([
    ['react-hooks/static-components', 'function Child({value}: {value: string}) { return <div>{value}</div>; } export function Parent({value}: {value: string}) { return <Child value={value}/>; }'],
    ['react-hooks/set-state-in-effect', 'import {useState} from "react"; export function C() { const [text,setText]=useState(""); return <button onClick={()=>setText("clicked")}>{text}</button>; }'],
    ['preserve-caught-error', 'export function f() { try { JSON.parse("x"); } catch (error) { throw new Error("Invalid JSON", {cause: error}); } }'],
  ])('does not report %s for its valid counterpart', async (rule, source) => {
    expect(await ruleIdsFor(source, TSX_PROBE)).not.toContain(rule);
  });
});

const REFRESH = 'react-refresh/only-export-components';

// These are the unchanged Dialog, app-registry and deferred-preview export
// shapes. Refresh 0.5.4 adds debt for them despite the identical configuration;
// a version/config snapshot alone does not protect the consuming convention.
describe('React Refresh preserves the established component export shapes', () => {
  it.each([
    ['primitive aliases', 'import * as Primitive from "@radix-ui/react-dialog"; export const Dialog = Primitive.Root; export function Content() { return <div/>; }'],
    ['lazy registry entries', 'import { lazy } from "react"; const Body = lazy(() => import("./Body")); export const registry = { Component: () => <Body/> };'],
    ['deferred components', 'import { lazyPage } from "./lazyPage"; export const Preview = lazyPage(() => import("./Preview")); export function Panel() { return <Preview/>; }'],
    ['literal constants', 'export const LIMIT = 5; export function Panel() { return <div/>; }'],
  ])('does not introduce a diagnostic for %s', async (_name, code) => {
    expect(await ruleIdsFor(code, TSX_PROBE)).not.toContain(REFRESH);
  });

  it('still errors on a component mixed with an ordinary function export', async () => {
    expect(await errorRuleIdsFor('export function helper() { return 1; } export function Panel() { return <div/>; }', TSX_PROBE)).toContain(REFRESH);
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
