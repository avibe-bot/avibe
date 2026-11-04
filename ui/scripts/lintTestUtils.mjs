// Test-only harness: real Oxlint CLI and real gate modules in a disposable UI.
// It must be outside node_modules: React Compiler intentionally skips dependency
// paths. Only this checkout's installed packages are linked into the fixture.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { measureLint } from './oxlintAdapter.mjs';

export const UI_ROOT = fileURLToPath(new URL('../', import.meta.url));
export const GATE_MODULES = ['lint-baseline.mjs', 'lintPolicy.mjs', 'browserGlobals.mjs', 'localizedUserText.mjs', 'lintPlugin.mjs', 'oxlintAdapter.mjs'];
export const readConfig = () => JSON.parse(fs.readFileSync(path.join(UI_ROOT, '.oxlintrc.json'), 'utf8'));

export function createLintTree() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'avibe-oxlint-test-'));
  fs.symlinkSync(path.join(UI_ROOT, 'node_modules'), path.join(root, 'node_modules'), 'dir');
  fs.mkdirSync(path.join(root, 'scripts'));
  fs.copyFileSync(path.join(UI_ROOT, '.oxlintrc.json'), path.join(root, '.oxlintrc.json'));
  for (const name of GATE_MODULES) fs.copyFileSync(path.join(UI_ROOT, 'scripts', name), path.join(root, 'scripts', name));
  fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ type: 'module', scripts: { lint: 'node scripts/lint-baseline.mjs' } }));
  return root;
}

export function writeSource(root, file, code) {
  fs.mkdirSync(path.dirname(path.join(root, file)), { recursive: true });
  fs.writeFileSync(path.join(root, file), code);
}

export function lintMessages(code, file = 'src/__probe__/probe.ts') {
  const root = createLintTree();
  try {
    writeSource(root, file, code);
    const { results, measurement } = measureLint(root);
    if (measurement.fatals.length || measurement.inlineViolations.length || measurement.policyGroups.length
      || measurement.coverage.missing.length || measurement.coverage.unwalked.length) {
      throw new Error(JSON.stringify(measurement));
    }
    return results.find((result) => result.filePath === path.join(root, file));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}
