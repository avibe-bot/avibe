import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { ESLint, Linter } from 'eslint';
import globals from 'globals';

import sharedConfig from '../eslint.config.js';
import { EXPECTED_BROWSER_GLOBALS } from './browserGlobals.mjs';
import {
  DEPENDENCY_ROOTS,
  EXPECTED_POLICY,
  coverageGaps,
  effectivePolicyOf,
  forbiddenInlineConfig,
  groupByDifferences,
  inlineConfigViolations,
  intendedFiles,
  policyDifferences,
  withoutRules,
} from './lintPolicy.mjs';

// What the gate measures, measured. Every case here is a way the lint run could
// look at less than it claims to while `eslint-baseline.json` stays green — the
// failure mode the ledger structurally cannot see, because it records what was
// found and never what was looked at.

const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));
const SCRIPTS_ROOT = fileURLToPath(new URL('./', import.meta.url));

let eslint;
beforeAll(() => {
  eslint = new ESLint();
});

// ─── the domain ──────────────────────────────────────────────────────────────

describe('intendedFiles walks the whole repo-owned TypeScript tree', () => {
  let tree;

  beforeAll(() => {
    tree = fs.mkdtempSync(path.join(os.tmpdir(), 'lint-domain-'));
    const write = (relative, body = 'export const x = 1;\n') => {
      fs.mkdirSync(path.join(tree, path.dirname(relative)), { recursive: true });
      fs.writeFileSync(path.join(tree, relative), body);
    };
    write('root.ts');
    write('Root.tsx');
    write('deeply/nested/under/many/levels/module.ts');
    write('src/component/Widget.tsx');
    write('notes.md', '# not code\n');
    write('script.js');
    write('node_modules/some-package/index.ts');
    write('dist/assets/bundle.ts');
    write('src/keep/real.ts');
    fs.symlinkSync(path.join(tree, 'src/keep'), path.join(tree, 'src/linkedDir'), 'dir');
    fs.symlinkSync(path.join(tree, 'root.ts'), path.join(tree, 'linked.ts'));
  });

  afterAll(() => {
    fs.rmSync(tree, { recursive: true, force: true });
  });

  it('finds every .ts and .tsx file at any depth, including the repo root', () => {
    expect(intendedFiles(tree)).toEqual([
      'Root.tsx',
      'deeply/nested/under/many/levels/module.ts',
      'linked.ts',
      'root.ts',
      'src/component/Widget.tsx',
      'src/keep/real.ts',
    ]);
  });

  it('treats symlinks the way ESLint does, so the two lists can be compared', async () => {
    // Disagreement here would show up as a permanent coverage failure with
    // nothing to fix, so the walk is pinned against the real enumerator: a
    // symlinked file is linted, a symlinked directory is not descended into.
    const linted = await new ESLint({ cwd: tree, overrideConfigFile: true, overrideConfig: sharedConfig }).lintFiles([
      '.',
    ]);
    const relative = linted.map((result) => path.relative(tree, result.filePath).split(path.sep).join('/'));
    expect(relative.filter((file) => file.endsWith('.ts') || file.endsWith('.tsx')).sort()).toEqual(intendedFiles(tree));
  });

  it('excludes exactly the two roots the declared policy already places outside the domain', () => {
    expect(DEPENDENCY_ROOTS).toEqual(['node_modules', 'dist']);
    expect(intendedFiles(tree).some((file) => file.startsWith('node_modules/') || file.startsWith('dist/'))).toBe(false);
    // Nothing else may be dropped: an extra exclusion is a hole in the invariant,
    // so the parameter exists only to make that visible in a test.
    expect(intendedFiles(tree, { excluded: [] })).toContain('node_modules/some-package/index.ts');
  });

  it('cannot be hung by a symlink cycle, because it never descends one', () => {
    fs.symlinkSync(tree, path.join(tree, 'src/loop'), 'dir');
    try {
      expect(intendedFiles(tree)).toContain('src/keep/real.ts');
    } finally {
      fs.rmSync(path.join(tree, 'src/loop'), { force: true, recursive: true });
    }
  });
});

describe('the real UI tree is what the gate measures', () => {
  it('covers the root config file the previous scope contract could not see', () => {
    // `basePath` narrowed the rule-bearing entry to `src/` with every field the
    // old hand-written contract listed left byte-identical, and `vite.config.ts`
    // silently stopped being linted. It is in the domain by construction now.
    expect(intendedFiles(UI_ROOT)).toContain('vite.config.ts');
  });

  it('covers nested sources without anyone listing them', () => {
    const found = intendedFiles(UI_ROOT);
    expect(found.length).toBeGreaterThan(100);
    expect(found.every((file) => file.endsWith('.ts') || file.endsWith('.tsx'))).toBe(true);
    expect(found.some((file) => file.split('/').length > 3)).toBe(true);
  });

  it('resolves exactly the pinned policy for every one of them', async () => {
    const drifted = [];
    for (const file of intendedFiles(UI_ROOT)) {
      const differences = policyDifferences(effectivePolicyOf(await eslint.calculateConfigForFile(path.join(UI_ROOT, file))));
      if (differences.length > 0) drifted.push({ file, differences });
    }
    expect(groupByDifferences(drifted)).toEqual([]);
  });
});

// ─── the policy ──────────────────────────────────────────────────────────────

const resolved = async (filePath = 'src/__probe__/probe.ts') =>
  effectivePolicyOf(await eslint.calculateConfigForFile(filePath));

describe('policyDifferences pins the whole effective policy, not a projection of it', () => {
  let actual;
  beforeAll(async () => {
    actual = await resolved();
  });

  it('accepts the policy the shared config actually resolves', () => {
    expect(policyDifferences(actual)).toEqual([]);
  });

  it('rejects a path no configuration matches at all', () => {
    expect(policyDifferences(null)).toEqual([
      'no ESLint configuration matches this file, so nothing about it is linted',
    ]);
  });

  it('rejects a rule that stopped resolving', () => {
    const { 'react-hooks/globals': _dropped, ...rules } = actual.rules;
    expect(policyDifferences({ ...actual, rules })).toEqual([
      '1 pinned rule(s) no longer resolve: react-hooks/globals',
    ]);
  });

  it('rejects a rule demoted to a warning', () => {
    const rules = { ...actual.rules, '@typescript-eslint/no-explicit-any': [1] };
    expect(policyDifferences({ ...actual, rules })).toEqual([
      'rule config changed — @typescript-eslint/no-explicit-any: expected [2], resolved [1]',
    ]);
  });

  // Severity alone was the projection this replaces. A preset can keep a rule at
  // error and loosen its options, enforcing less with the severity map unchanged.
  it('rejects options loosened while the severity stays at error', () => {
    const rules = {
      ...actual.rules,
      '@typescript-eslint/no-unused-vars': [2, { argsIgnorePattern: '.*', varsIgnorePattern: '.*' }],
    };
    const lines = policyDifferences({ ...actual, rules });
    expect(lines).toHaveLength(1);
    expect(lines[0]).toContain('rule config changed — @typescript-eslint/no-unused-vars');
  });

  it('rejects a rule nobody pinned appearing from an upgrade', () => {
    const rules = { ...actual.rules, 'brand-new/rule': [2] };
    expect(policyDifferences({ ...actual, rules })).toEqual(['1 unpinned rule(s) resolve: brand-new/rule']);
  });

  it('names each drifting boundary that can stop rules running', () => {
    const cases = {
      parser: 'espree',
      ecmaVersion: 5,
      sourceType: 'script',
      processor: 'some-plugin/markdown',
      parserOptions: { project: './tsconfig.json' },
      plugins: ['@'],
      linterOptions: { reportUnusedDisableDirectives: 0 },
    };
    for (const [key, value] of Object.entries(cases)) {
      const lines = policyDifferences({ ...actual, [key]: value });
      expect(lines, key).toHaveLength(1);
      expect(lines[0], key).toContain(`${key}: expected`);
    }
  });

  it('holds the browser globals to the contract the config declares', () => {
    expect(policyDifferences({ ...actual, globals: {} })).toEqual([
      expect.stringContaining(`${Object.keys(EXPECTED_BROWSER_GLOBALS).length} expected global(s) absent`),
    ]);
    expect(policyDifferences({ ...actual, globals: { ...actual.globals, __injected__: 'readonly' } })).toEqual([
      '1 unexpected global(s): __injected__',
    ]);
    expect(policyDifferences({ ...actual, globals: { ...actual.globals, window: 'writable' } })).toEqual([
      '1 global(s) changed writability: window',
    ]);
  });

  it('reports a one-name catalog change as the only difference there is', () => {
    // What a `globals` upgrade actually looks like in the resolved config: one
    // name appears, one disappears, or one stops being readonly. Each has to
    // surface on its own, with the rest of the policy reading as unchanged —
    // otherwise the failure is unattributable and gets waved through.
    const { structuredClone: _dropped, ...withoutOne } = actual.globals;
    const cases = [
      ['upstream addition', { ...actual.globals, upstreamAddition: false }, '1 unexpected global(s): upstreamAddition'],
      ['upstream removal', withoutOne, '1 expected global(s) absent: structuredClone'],
      ['loosened to writable', { ...actual.globals, structuredClone: true }, '1 global(s) changed writability: structuredClone'],
    ];
    for (const [label, catalog, expected] of cases) {
      expect(policyDifferences({ ...actual, globals: catalog }), label).toEqual([expected]);
    }
  });

  it('pins the resolved parser rather than whatever the config happens to name', () => {
    expect(EXPECTED_POLICY.parser).toBe('typescript-eslint/parser');
    expect(actual.parser).toBe(EXPECTED_POLICY.parser);
  });
});

describe('the expected browser globals are a committed value, not a read of the dependency', () => {
  // The last hole in the policy contract, and a different shape from the earlier
  // ones: the subject was complete — all 1169 names and writability values were
  // compared — but the *reference* was `globals.browser` itself, the same export
  // `eslint.config.js` resolves from. On that one axis the comparison was
  // `X === X`, so an upstream catalog change moved both sides together and passed,
  // and the only trace was a version bump in the lockfile.

  const importsOf = (filename) => {
    const linter = new Linter();
    const code = fs.readFileSync(path.join(SCRIPTS_ROOT, filename), 'utf8');
    linter.verify(code, { languageOptions: { ecmaVersion: 'latest', sourceType: 'module' } }, { filename });
    const sourceCode = linter.getSourceCode();
    if (sourceCode === null) throw new Error(`${filename} did not parse`);
    return sourceCode.ast.body.filter((node) => node.type === 'ImportDeclaration').map((node) => node.source.value);
  };

  it('does not move when the installed catalog moves under it', () => {
    const snapshot = { ...EXPECTED_POLICY.globals };
    const original = { structuredClone: globals.browser.structuredClone, window: globals.browser.window };
    try {
      globals.browser.upstreamAddition = false;
      delete globals.browser.structuredClone;
      globals.browser.window = true;

      expect(EXPECTED_POLICY.globals).toEqual(snapshot);
      expect(EXPECTED_POLICY.globals).not.toHaveProperty('upstreamAddition');
      expect(EXPECTED_POLICY.globals.structuredClone).toBe(false);
      expect(EXPECTED_POLICY.globals.window).toBe(false);
    } finally {
      delete globals.browser.upstreamAddition;
      Object.assign(globals.browser, original);
    }
  });

  it('is not the installed catalog, by identity either', () => {
    expect(EXPECTED_BROWSER_GLOBALS).not.toBe(globals.browser);
    expect(EXPECTED_POLICY.globals).toBe(EXPECTED_BROWSER_GLOBALS);
  });

  it('is reached without the policy modules importing the dependency at all', () => {
    // A copy taken at load time would pass the mutation case above and still move
    // on every upgrade, so independence is also asserted where it is decided: the
    // module graph. Nothing in the expected side may reach `globals`.
    expect(importsOf('browserGlobals.mjs')).toEqual([]);
    expect(importsOf('lintPolicy.mjs')).not.toContain('globals');
  });

  it('is checked against the installed catalog in exactly one place, loudly', () => {
    // A snapshot has to be a snapshot *of* something. This is that statement, and
    // it is the whole reason the snapshot may be trusted as a reference: when the
    // dependency changes, this fails by name here instead of passing silently
    // everywhere.
    expect(EXPECTED_BROWSER_GLOBALS).toEqual(globals.browser);
  });
});

describe('a scope narrowing takes files out of the policy, and that is what fails', () => {
  const narrow = (mutate) =>
    new ESLint({
      overrideConfigFile: true,
      overrideConfig: sharedConfig.map((entry) =>
        entry && typeof entry === 'object' && 'files' in entry ? mutate(entry) : entry,
      ),
    });

  it('catches a basePath that quietly drops the repo root', async () => {
    // The exact defect: every `{name, files, ignores}` field stays byte-identical.
    const narrowed = narrow((entry) => ({ ...entry, basePath: 'src' }));
    expect(policyDifferences(effectivePolicyOf(await narrowed.calculateConfigForFile('src/App.tsx')))).toEqual([]);
    expect(policyDifferences(effectivePolicyOf(await narrowed.calculateConfigForFile('vite.config.ts')))).not.toEqual([]);
  });

  it('catches an ignores that exempts one nested subtree', async () => {
    const narrowed = narrow((entry) => ({ ...entry, ignores: ['src/lib/**'] }));
    expect(policyDifferences(effectivePolicyOf(await narrowed.calculateConfigForFile('src/App.tsx')))).toEqual([]);
    expect(
      policyDifferences(effectivePolicyOf(await narrowed.calculateConfigForFile('src/lib/useLatestRef.ts'))),
    ).not.toEqual([]);
  });
});

describe('groupByDifferences keeps a config-wide drift readable', () => {
  const drift = ['parser: expected "typescript-eslint/parser", resolved "espree"'];

  it('folds an identical failure on many files into one entry', () => {
    const perFile = ['src/a.ts', 'src/b.ts', 'src/c.ts'].map((file) => ({ file, differences: drift }));
    expect(groupByDifferences(perFile)).toEqual([{ differences: drift, files: ['src/a.ts', 'src/b.ts', 'src/c.ts'] }]);
  });

  it('keeps genuinely different failures apart', () => {
    const other = ['1 pinned rule(s) no longer resolve: no-var'];
    expect(
      groupByDifferences([
        { file: 'src/b.ts', differences: other },
        { file: 'src/a.ts', differences: drift },
      ]),
    ).toEqual([
      { differences: drift, files: ['src/a.ts'] },
      { differences: other, files: ['src/b.ts'] },
    ]);
  });

  it('drops files that resolved the policy exactly', () => {
    expect(groupByDifferences([{ file: 'src/a.ts', differences: [] }])).toEqual([]);
  });
});

// ─── the coverage ────────────────────────────────────────────────────────────

describe('coverageGaps compares the domain with what ESLint opened, both ways', () => {
  it('accepts a run that opened everything in the domain', () => {
    expect(coverageGaps({ intended: ['src/a.ts'], lintedFiles: ['src/a.ts', 'eslint.config.js'] })).toEqual({
      empty: false,
      missing: [],
      unwalked: [],
    });
  });

  it('names a nested intended file the run never opened', () => {
    expect(
      coverageGaps({ intended: ['src/a.ts', 'src/deep/nested/b.tsx'], lintedFiles: ['src/a.ts'] }).missing,
    ).toEqual(['src/deep/nested/b.tsx']);
  });

  it('names a linted TypeScript file the walk missed, so the walk cannot go blind', () => {
    expect(coverageGaps({ intended: ['src/a.ts'], lintedFiles: ['src/a.ts', 'src/hidden.tsx'] }).unwalked).toEqual([
      'src/hidden.tsx',
    ]);
  });

  it('refuses to pass vacuously when the walk produced nothing', () => {
    expect(coverageGaps({ intended: [], lintedFiles: [] }).empty).toBe(true);
  });
});

// ─── the inline escape hatch ─────────────────────────────────────────────────

const parseOnly = withoutRules(sharedConfig);

function violationsIn(code, filename = 'src/__probe__/probe.ts') {
  const linter = new Linter();
  linter.verify(code, parseOnly, { filename, allowInlineConfig: false });
  return inlineConfigViolations(linter.getSourceCode());
}

describe('inline configuration outside the eslint-disable family is rejected', () => {
  // Neither tally can see these: an inline severity override produces no error to
  // count and no suppressed message either, so the rule simply stops applying to
  // that file with nothing recorded anywhere.
  it('rejects an inline rule switched off', () => {
    const found = violationsIn('/* eslint @typescript-eslint/no-explicit-any: "off" */\nexport const f = (v: any) => v;\n');
    expect(found).toHaveLength(1);
    expect(found[0]).toMatchObject({ line: 1, column: 1 });
    expect(found[0].text).toContain('no-explicit-any');
  });

  it('rejects an inline rule demoted to a warning', () => {
    expect(violationsIn('/* eslint @typescript-eslint/no-explicit-any: "warn" */\nexport const f = (v: any) => v;\n')).toHaveLength(1);
  });

  it('rejects the declaration directives on the same ground', () => {
    expect(violationsIn('/* global someInjectedThing */\nexport const f = 1;\n')).toHaveLength(1);
    expect(violationsIn('/* globals a, b */\nexport const f = 1;\n')).toHaveLength(1);
    expect(violationsIn('/* exported f */\nexport const f = 1;\n')).toHaveLength(1);
  });

  it('allows every spelling of a suppression, which the ledger does freeze', () => {
    expect(violationsIn('/* eslint-disable @typescript-eslint/no-explicit-any */\nexport const f = (v: any) => v;\n')).toEqual([]);
    expect(violationsIn('// eslint-disable-next-line @typescript-eslint/no-explicit-any\nexport const f = (v: any) => v;\n')).toEqual([]);
    expect(violationsIn('export const f = (v: any) => v; // eslint-disable-line @typescript-eslint/no-explicit-any\n')).toEqual([]);
    expect(
      violationsIn('/* eslint-disable no-var */\nvar a = 1;\n/* eslint-enable no-var */\nexport const f = a;\n'),
    ).toEqual([]);
  });

  it('leaves an ordinary comment alone', () => {
    expect(violationsIn('// this mentions eslint but configures nothing\nexport const f = 1;\n')).toEqual([]);
  });

  it('still lets an allowed suppression reach the ledger, which is what freezes it', async () => {
    const [result] = await eslint.lintText(
      '// eslint-disable-next-line @typescript-eslint/no-explicit-any\nexport const f = (v: any) => v;\n',
      { filePath: 'src/__probe__/probe.ts' },
    );
    expect(result.suppressedMessages.map((message) => message.ruleId)).toEqual(['@typescript-eslint/no-explicit-any']);
    expect(result.messages).toEqual([]);
  });
});

describe('forbiddenInlineConfig sweeps the whole domain', () => {
  let tree;

  beforeAll(() => {
    tree = fs.mkdtempSync(path.join(os.tmpdir(), 'lint-inline-'));
    fs.writeFileSync(path.join(tree, 'clean.ts'), 'export const a = 1;\n');
    fs.writeFileSync(path.join(tree, 'suppressed.ts'), '/* eslint-disable no-var */\nexport const b = 1;\n');
    fs.writeFileSync(path.join(tree, 'override.ts'), '/* eslint no-var: "off" */\nexport const c = 1;\n');
  });

  afterAll(() => {
    fs.rmSync(tree, { recursive: true, force: true });
  });

  it('reports only the file that rewrote policy inline', () => {
    const found = forbiddenInlineConfig({
      root: tree,
      files: ['clean.ts', 'override.ts', 'suppressed.ts'],
      config: parseOnly,
      linter: new Linter(),
    });
    expect(found.map(({ file }) => file)).toEqual(['override.ts']);
  });

  it('reports a file it could not parse rather than passing over it', () => {
    fs.writeFileSync(path.join(tree, 'broken.ts'), 'export const = ;\n');
    const found = forbiddenInlineConfig({
      root: tree,
      files: ['broken.ts'],
      config: parseOnly,
      linter: new Linter(),
    });
    expect(found).toHaveLength(1);
    expect(found[0].file).toBe('broken.ts');
  });
});

describe('withoutRules keeps the real config and drops only rule execution', () => {
  it('empties every rule map while leaving scope and language intact', () => {
    const stripped = withoutRules(sharedConfig);
    expect(stripped).toHaveLength(sharedConfig.length);
    for (const [index, entry] of stripped.entries()) {
      if ('rules' in entry) expect(entry.rules).toEqual({});
      expect(entry.files).toEqual(sharedConfig[index].files);
      expect(entry.ignores).toEqual(sharedConfig[index].ignores);
      expect(entry.languageOptions).toEqual(sharedConfig[index].languageOptions);
    }
  });

  it('does not mutate the shared config it was handed', () => {
    const before = JSON.stringify(sharedConfig.map((entry) => Object.keys(entry.rules ?? {}).length));
    withoutRules(sharedConfig);
    expect(JSON.stringify(sharedConfig.map((entry) => Object.keys(entry.rules ?? {}).length))).toBe(before);
  });
});
