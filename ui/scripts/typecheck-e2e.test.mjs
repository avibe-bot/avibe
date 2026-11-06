import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';

const CHECKER = fileURLToPath(new URL('./typecheck-e2e.mjs', import.meta.url));
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'typecheck-e2e-'));
afterAll(() => fs.rmSync(directory, { recursive: true, force: true }));

// One e2e directory per call, each holding the named files.
function tree(name, suites) {
  const root = path.join(directory, name);
  for (const [suite, files] of Object.entries(suites)) {
    fs.mkdirSync(path.join(root, suite), { recursive: true });
    for (const [file, source] of Object.entries(files)) fs.writeFileSync(path.join(root, suite, file), source);
  }
  fs.mkdirSync(root, { recursive: true });
  return root;
}
const tsconfig = (spec) => JSON.stringify({ compilerOptions: { strict: true, noEmit: true, types: [] }, files: [spec] });
const clean = { 'a.spec.ts': 'export const count: number = 1;\n', 'tsconfig.json': tsconfig('a.spec.ts') };
const broken = { 'a.spec.ts': "export const count: number = 'one';\n", 'tsconfig.json': tsconfig('a.spec.ts') };

function check(root) {
  const result = spawnSync(process.execPath, [CHECKER, root], { encoding: 'utf8', timeout: 60000 });
  if (result.error) throw result.error;
  return { status: result.status, output: `${result.stdout}\n${result.stderr}` };
}

// CI runs browser cases for only some suites; this check is what keeps the rest compiling.
describe('the e2e type check', () => {
  it('fails on a type error in any suite, and prints it', () => {
    const result = check(tree('one-broken', { good: clean, bad: broken }));
    expect(result.output).toMatch(/ok {4}.*good/);
    expect(result.output).toMatch(/FAIL {2}.*bad/);
    expect(result.output).toContain("Type 'string' is not assignable to type 'number'");
    expect(result.status).not.toBe(0);
  });

  it('fails on a suite with specs but no tsconfig, naming it', () => {
    const result = check(tree('untyped', {
      good: clean,
      untyped: { 'a.spec.ts': 'export {};\n' },
      'untyped-tsx': { 'a.spec.tsx': 'export {};\n' },
    }));
    expect(result.output).toMatch(/untyped has browser specs but no tsconfig\.json/);
    expect(result.output).toMatch(/untyped-tsx has browser specs but no tsconfig\.json/);
    expect(result.status).not.toBe(0);
  });

  it('passes when every suite checks, leaving out helpers without specs and hidden output', () => {
    const result = check(tree('passing', {
      good: clean,
      support: { 'helper.ts': "export const count: number = 'one';\n" },
      '.artifacts': { 'a.spec.ts': 'export {};\n' },
    }));
    expect(result.output).toContain('1 e2e suites type-check');
    expect(result.status).toBe(0);
  });

  it('fails when it finds no suite at all', () => {
    const result = check(tree('empty', {}));
    expect(result.output).toContain('No e2e suite found');
    expect(result.status).not.toBe(0);
  });
});
