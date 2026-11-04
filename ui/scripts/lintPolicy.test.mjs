import fs from 'node:fs';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { parseSync } from 'oxc-parser';
import { EXPECTED_BROWSER_GLOBALS } from './browserGlobals.mjs';
import {
  DEPENDENCY_ROOTS, EXPECTED_CONFIG, OXLINT_VERSION, coverageGaps,
  inlineConfigViolations, intendedFiles, policyDifferences,
} from './lintPolicy.mjs';
import { measureLint } from './oxlintAdapter.mjs';
import { createLintTree, lintMessages, readConfig, writeSource } from './lintTestUtils.mjs';

// Impose the Windows process boundary even on Unix CI; all allowed calls still
// run the real engine. Without batching, the large-domain fixture cannot spawn.
vi.mock('node:child_process', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, spawnSync(command, args, options) {
    if ([command, ...args].join(' ').length > 32767) {
      throw new Error('CreateProcess command line exceeds 32767 characters');
    }
    return actual.spawnSync(command, args, options);
  } };
});

// The ledger records findings, not coverage. These are behavioral probes of
// every boundary that could otherwise make fewer findings look like less debt.
let tree;
beforeEach(() => { tree = createLintTree(); });
afterEach(() => fs.rmSync(tree, { recursive: true, force: true }));
const setConfig = (config) => fs.writeFileSync(path.join(tree, '.oxlintrc.json'), JSON.stringify(config));

describe('the declared domain and the real Oxlint discovery agree', () => {
  it('includes root/deep TS/TSX and symlinked files, but not linked directories or dependency/build roots', () => {
    for (const file of ['root.ts', 'Root.tsx', 'deep/nested/module.ts', 'src/keep/real.ts', 'src/nested/node_modules/pkg/index.ts', 'dist/assets/bundle.ts']) {
      writeSource(tree, file, 'export const x = 1;');
    }
    writeSource(tree, 'script.js', 'export const x = 1;');
    fs.symlinkSync(path.join(tree, 'root.ts'), path.join(tree, 'linked.ts'));
    fs.symlinkSync(path.join(tree, 'src/keep'), path.join(tree, 'src/linkedDir'), 'dir');
    fs.symlinkSync(tree, path.join(tree, 'src/loop'), 'dir');
    expect(intendedFiles(tree)).toEqual(['Root.tsx', 'deep/nested/module.ts', 'linked.ts', 'root.ts', 'src/keep/real.ts']);
    const measured = measureLint(tree);
    expect(measured.measurement.coverage).toEqual({ empty: false, missing: [], unwalked: [] });
    expect(measured.results.map((result) => path.relative(tree, result.filePath)).sort()).toEqual(intendedFiles(tree));
    expect(DEPENDENCY_ROOTS).toEqual(['node_modules', 'dist']);
    expect(intendedFiles(tree, { excluded: [] })).toContain('src/nested/node_modules/pkg/index.ts');
  });

  it('does not let ignore files or nested configs remove a new source from enforcement', () => {
    writeSource(tree, 'deep/nested/new.ts', 'export const f = (value: any) => value;');
    writeSource(tree, '.gitignore', 'deep/\n');
    writeSource(tree, '.eslintignore', 'deep/\n');
    writeSource(tree, 'deep/.oxlintrc.json', '{"ignorePatterns":["**/*"],"rules":{"typescript/no-explicit-any":"off"}}');
    const measured = measureLint(tree);
    expect(measured.measurement.coverage.missing).toEqual([]);
    expect(measured.results[0].messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ ruleId: '@typescript-eslint/no-explicit-any', severity: 2 }),
    ]));
  });

  it('measures the complete large domain within the Windows command-line limit', () => {
    const names = Array.from({ length: 200 }, (_, i) => `src/${'long-path-'.repeat(16)}/file${String(i).padStart(3, '0')}.ts`);
    for (const file of names) writeSource(tree, file, 'export const x = 1;');
    const offending = 'export const f = (v: any) => v;';
    for (const file of [names[0], names.at(-1)]) {
      writeSource(tree, file, `// oxlint-disable-next-line typescript/no-explicit-any\n${offending}\nexport const g = (v: any) => v;`);
    }
    expect(names.join(' ').length).toBeGreaterThan(32767);
    const { results, measurement } = measureLint(tree);
    expect(measurement.coverage).toEqual({ empty: false, missing: [], unwalked: [] });
    expect(measurement.fatals).toEqual([]);
    expect(results).toHaveLength(names.length);
    expect(results.flatMap((result) => result.messages).filter((message) => message.severity === 2)).toHaveLength(2);
    expect(results.flatMap((result) => result.suppressedMessages)).toHaveLength(2);
  }, 15000);

  it('fails closed on an empty domain', () => {
    expect(measureLint(tree).measurement.coverage.empty).toBe(true);
  });
});

describe('the complete policy is independently pinned', () => {
  it('accepts the checked-in config, with a pinned engine and the existing browser catalog', () => {
    const config = readConfig();
    expect(policyDifferences(config)).toEqual([]);
    expect(config).not.toBe(EXPECTED_CONFIG);
    expect(OXLINT_VERSION).toBe('1.91.0');
    expect(config.globals).toEqual(Object.fromEntries(Object.entries(EXPECTED_BROWSER_GLOBALS)
      .map(([key, writable]) => [key, writable ? 'writable' : 'readonly'])));
  });

  it.each([
    ['missing rule', (c) => { delete c.overrides[0].rules['react-hooks-js/globals']; }],
    ['demoted severity', (c) => { c.overrides[0].rules['typescript/no-explicit-any'] = [1]; }],
    ['loosened options', (c) => { c.overrides[0].rules['eslint/no-unused-vars'] = [2, { varsIgnorePattern: '.*' }]; }],
    ['new rule', (c) => { c.overrides[0].rules['eslint/no-console'] = [2]; }],
    ['narrowed files', (c) => { c.overrides[0].files = ['src/**/*.ts']; }],
    ['extra override', (c) => { c.overrides.push({ files: ['src/lib/**'], rules: { 'typescript/no-explicit-any': 'off' } }); }],
    ['ignored subtree', (c) => { c.ignorePatterns.push('src/lib/**'); }],
    ['basePath escape', (c) => { c.overrides[0].basePath = 'src'; }],
    ['plugin replacement', (c) => { c.jsPlugins[0].specifier = 'other-plugin'; }],
    ['plugin removal', (c) => { c.jsPlugins.pop(); }],
    ['native plugin removal', (c) => { c.plugins = []; }],
    ['inherited policy', (c) => { c.extends = ['./other.json']; }],
    ['default preset', (c) => { c.categories.correctness = 'warn'; }],
    ['unused-disable reporting', (c) => { c.options.reportUnusedDisableDirectives = 'off'; }],
    ['disable interpretation', (c) => { c.options.respectEslintDisableDirectives = false; }],
    ['environment widening', (c) => { c.env.builtin = true; }],
    ['global addition', (c) => { c.globals.injected = 'readonly'; }],
    ['global removal', (c) => { delete c.globals.structuredClone; }],
    ['global writability', (c) => { c.globals.window = 'writable'; }],
  ])('rejects %s before executing or baselining any rule', (_name, mutate) => {
    const config = readConfig();
    mutate(config);
    expect(policyDifferences(config).length).toBeGreaterThan(0);
    setConfig(config);
    writeSource(tree, 'src/clean.ts', 'export const x = 1;');
    const measured = measureLint(tree);
    expect(measured.measurement.policyGroups).toHaveLength(1);
    expect(measured.results).toEqual([]);
  });

  it('reports a one-name catalog change without unrelated noise', () => {
    const config = readConfig();
    config.globals.structuredClone = 'writable';
    expect(policyDifferences(config)).toEqual(['globals.structuredClone: expected "readonly", resolved "writable"']);
    expect(EXPECTED_CONFIG.globals.structuredClone).toBe('readonly');
  });

  it('rejects a missing config and a second hidden suppression ledger', () => {
    expect(policyDifferences(null)).toEqual(['no Oxlint configuration was loaded']);
    writeSource(tree, 'root.ts', 'export const x = 1;');
    writeSource(tree, 'oxlint-suppressions.json', '{}');
    expect(measureLint(tree).measurement.policyGroups[0].differences).toContain('an untracked Oxlint suppression ledger exists');
  });
});

describe('coverage comparison is two-way and cannot pass vacuously', () => {
  it.each([
    [['src/a.ts'], ['src/a.ts', 'script.js'], { empty: false, missing: [], unwalked: [] }],
    [['src/a.ts', 'nested/b.tsx'], ['src/a.ts'], { empty: false, missing: ['nested/b.tsx'], unwalked: [] }],
    [['src/a.ts'], ['src/a.ts', 'hidden.tsx'], { empty: false, missing: [], unwalked: ['hidden.tsx'] }],
    [[], [], { empty: true, missing: [], unwalked: [] }],
  ])('compares %j with %j', (intended, lintedFiles, expected) => {
    expect(coverageGaps({ intended, lintedFiles })).toEqual(expected);
  });
});

function inline(code) {
  const parsed = parseSync('probe.ts', code);
  expect(parsed.errors).toEqual([]);
  return inlineConfigViolations(parsed.comments, code);
}

describe('only parsed suppression comments can hide a diagnostic', () => {
  it.each([
    '/* eslint @typescript-eslint/no-explicit-any: "off" */',
    '/* eslint @typescript-eslint/no-explicit-any: "warn" */',
    '/* oxlint typescript/no-explicit-any: "off" */',
    '/* global injected */', '/* globals a, b */', '/* exported f */',
  ])('rejects policy bypass %s', (comment) => {
    expect(inline(`${comment}\nexport const f = 1;`)).toEqual([
      expect.objectContaining({ line: 1, column: 1 }),
    ]);
  });

  it('does not mistake prose or string/template content for directives', () => {
    expect(inline('// global default is inherited\n// exported separately for callers\nexport const text = `/* eslint no-var: off */`;')).toEqual([]);
  });

  it.each(['eslint', 'oxlint'])('measures %s suppressed errors independently of visible errors', (prefix) => {
    for (const code of [
      `// ${prefix}-disable-next-line typescript/no-explicit-any\nexport const f = (v: any) => v;`,
      `export const f = (v: any) => v; // ${prefix}-disable-line typescript/no-explicit-any`,
      `/* ${prefix}-disable typescript/no-explicit-any */\nexport const f = (v: any) => v;\n/* ${prefix}-enable typescript/no-explicit-any */`,
    ]) {
      expect(inline(code)).toEqual([]);
      const result = lintMessages(code);
      expect(result.messages.filter((message) => message.ruleId)).toEqual([]);
      expect(result.suppressedMessages.map((message) => message.ruleId)).toEqual(['@typescript-eslint/no-explicit-any']);
    }
  });

  it('preserves UTF-8 offsets and accounts for suppressed JS-plugin warnings', () => {
    const result = lintMessages(`const label = '中文';
import { useEffect } from 'react';
export function C({ value }) {
  useEffect(() => { console.log(value, label); },
  // eslint-disable-next-line react-hooks-js/exhaustive-deps
  []);
  return null;
}`, 'src/Probe.tsx');
    expect(result.suppressedMessages).toEqual([
      expect.objectContaining({ ruleId: 'react-hooks/exhaustive-deps', severity: 1 }),
    ]);
  });

  it('measures file-wide disables rather than treating a silent file as clean', () => {
    const result = lintMessages('/* oxlint-disable */\nexport const f = (v: any) => v;');
    expect(result.messages.filter((message) => message.ruleId)).toEqual([]);
    expect(result.suppressedMessages.map((message) => message.ruleId)).toContain('@typescript-eslint/no-explicit-any');
  });
});

describe('fatal failures cannot be recorded as debt', () => {
  it('rejects an unparseable file instead of silently dropping it', () => {
    writeSource(tree, 'broken.ts', '// 😀\r\n\r\nexport const = ;');
    expect(measureLint(tree).measurement.fatals).toEqual([
      expect.objectContaining({ filePath: 'broken.ts', line: 3 }),
    ]);
  });

  it('surfaces a JS-plugin exception rather than returning an empty successful result', () => {
    writeSource(tree, 'src/Probe.tsx', 'export const Probe = () => <button>Save</button>;');
    writeSource(tree, 'scripts/localizedUserText.mjs', 'export default { create() { throw new Error("test plugin failure"); } };');
    const { measurement } = measureLint(tree);
    expect(measurement.fatals).toEqual([
      expect.objectContaining({ message: expect.stringContaining('test plugin failure') }),
    ]);
  });
});
