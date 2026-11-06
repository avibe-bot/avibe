// Type-checks every browser suite under e2e/, whether or not CI runs its cases.
//
//   node scripts/typecheck-e2e.mjs [e2e directory]
//
// The browser lanes check their own suites beside Playwright (scripts/e2e-suite.mjs),
// so a suite outside the lanes was checked by nobody: a prop a shared component
// made required broke one fixture unnoticed. This checks them all, and fails when
// a suite cannot be checked at all.
//
// A suite is a directory directly under e2e/ that holds a `*.spec.ts` or
// `*.spec.tsx`. Each needs a tsconfig.json beside its specs; a directory with a
// tsconfig.json is checked whether or not it has specs. Shared helpers without
// specs (e2e/support) are checked through the suites that import them.
//
// Checks run four at a time, the CI runner's vCPU count; more would only
// contend. Each check's output is held and printed once it finishes, so outputs
// never interleave.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';

const require = createRequire(import.meta.url);
const TSC = path.join(path.dirname(require.resolve('typescript/package.json')), 'bin/tsc');
const CONCURRENCY = Math.min(4, os.availableParallelism());

const root = path.resolve(process.argv[2] ?? 'e2e');
const label = (file) => path.relative(process.cwd(), file) || '.';

const suites = [];
const missing = [];
for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
  if (!entry.isDirectory() || entry.name.startsWith('.')) continue;
  const directory = path.join(root, entry.name);
  const names = fs.readdirSync(directory);
  const tsconfig = path.join(directory, 'tsconfig.json');
  if (names.includes('tsconfig.json')) suites.push(tsconfig);
  else if (names.some((name) => /\.spec\.tsx?$/.test(name))) missing.push(directory);
}
suites.sort();

function check(tsconfig) {
  return new Promise((resolve) => {
    const started = performance.now();
    const child = spawn(process.execPath, [TSC, '-p', tsconfig, '--noEmit'], { stdio: ['ignore', 'pipe', 'pipe'] });
    let output = '';
    child.stdout.on('data', (chunk) => (output += chunk));
    child.stderr.on('data', (chunk) => (output += chunk));
    const finish = (code, extra = '') => {
      const seconds = ((performance.now() - started) / 1000).toFixed(1);
      if (code === 0) {
        console.log(`ok    ${label(tsconfig)} (${seconds} s)`);
      } else {
        console.error(`FAIL  ${label(tsconfig)} (${seconds} s)\n${output}${extra}`);
      }
      resolve(code === 0);
    };
    child.on('error', (error) => finish(1, `${error.message}\n`));
    child.on('close', (code) => finish(code ?? 1));
  });
}

const queue = [...suites];
const results = [];
await Promise.all(
  Array.from({ length: CONCURRENCY }, async () => {
    while (queue.length) results.push(await check(queue.shift()));
  }),
);

for (const directory of missing) {
  console.error(`FAIL  ${label(directory)} has browser specs but no tsconfig.json, so nothing type-checks them`);
}
const failed = results.filter((passed) => !passed).length + missing.length;
if (suites.length + missing.length === 0) {
  console.error(`No e2e suite found under ${label(root)}`);
  process.exitCode = 1;
} else if (failed) {
  console.error(`${failed} of ${suites.length + missing.length} e2e suites failed their type check`);
  process.exitCode = 1;
} else {
  console.log(`${suites.length} e2e suites type-check`);
}
