import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { expect, test } from 'vitest';

const uiRoot = fileURLToPath(new URL('..', import.meta.url));
const collectedSpecs = (suite) => [
  ...(suite.specs ?? []),
  ...(suite.suites ?? []).flatMap(collectedSpecs),
];

function collect(config) {
  // List mode loads the real config/specs but never starts fixtures or webServer.
  const report = JSON.parse(execFileSync(process.execPath, [
    'node_modules/@playwright/test/cli.js', 'test', '--config', config,
    '--list', '--reporter=json',
  ], {
    cwd: uiRoot,
    env: {
      ...process.env,
      VIBE_E2E_BASE_URL: 'http://127.0.0.1:9',
      VIBE_E2E_DESTRUCTIVE_TARGET: 'http://127.0.0.1:9',
    },
    encoding: 'utf8',
    timeout: 30_000,
    maxBuffer: 8 * 1024 * 1024,
  }));
  expect(report.errors).toEqual([]);
  return collectedSpecs(report);
}

test('live E2E discovery never collects the model catalog fixture', () => {
  const specs = collect('playwright.config.ts');
  expect(specs.length).toBeGreaterThan(0);
  expect(specs.filter((spec) => spec.file.startsWith('model-catalog/'))).toEqual([]);
}, 35_000);

test('isolated catalog discovery retains all scenario-labelled browser cases', () => {
  const specs = collect('playwright.model-catalog.config.ts');
  expect(specs.flatMap((spec) => spec.tests)).toHaveLength(36);
  for (const spec of specs) {
    expect(spec.title).toContain('MH-MENU-COMPOSE-001');
    expect(spec.file).toBe('catalog.spec.ts');
  }
}, 35_000);
