import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { afterAll, describe, expect, it } from 'vitest';

const RUNNER = fileURLToPath(new URL('./e2e-suite.mjs', import.meta.url));
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-suite-'));
afterAll(() => fs.rmSync(directory, { recursive: true, force: true }));

const write = (name, value) => {
  fs.writeFileSync(path.join(directory, name), value);
  return path.join(directory, name);
};
const project = (name, source) => {
  write(`${name}.ts`, source);
  return write(`${name}.tsconfig.json`, JSON.stringify({ compilerOptions: { strict: true, noEmit: true }, files: [`${name}.ts`] }));
};
const broken = project('broken', "export const count: number = 'one';\n");
const clean = project('clean', 'export const count: number = 1;\n');
// No spec files: `--list --pass-with-no-tests` makes Playwright succeed without a browser,
// and only when the runner forwards the arguments after the config.
const emptySuite = write('empty.config.mjs', 'export default { testDir: "." };\n');
const listOnly = ['--list', '--pass-with-no-tests'];

function suite(...args) {
  const result = spawnSync(process.execPath, [RUNNER, ...args], { encoding: 'utf8', timeout: 30000 });
  if (result.error) throw result.error;
  return { status: result.status, output: `${result.stdout}\n${result.stderr}` };
}

// The type check now runs beside the browser run instead of gating it, so its
// verdict reaches the exit code only through the runner.
describe('one browser suite and its type check', () => {
  it('fails on a type error even when every browser case passes, and prints the error', () => {
    const result = suite(broken, emptySuite, ...listOnly);
    expect(result.output).toContain('Total: 0 tests in 0 files');
    expect(result.output).toContain("Type 'string' is not assignable to type 'number'");
    expect(result.status).not.toBe(0);
  });

  it('fails when the browser run fails even though the types check', () => {
    const result = suite(clean, path.join(directory, 'missing.config.mjs'), ...listOnly);
    expect(result.output).toContain('no type errors');
    expect(result.status).not.toBe(0);
  });

  it('passes only when both pass', () => {
    expect(suite(clean, emptySuite, ...listOnly).status).toBe(0);
  });
});
