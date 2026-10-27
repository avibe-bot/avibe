// Runs one Playwright suite and its TypeScript check side by side.
//
//   node scripts/e2e-suite.mjs <tsconfig> <playwright config> [playwright args...]
//
// Each `npm run test:<suite>` used to run `tsc -p <tsconfig> --noEmit &&
// playwright test --config <config>`: the browser run waited for a type check it
// shares nothing with. Checking a suite's specs, fixtures and the app sources they
// import takes 20 to 35 seconds on a CI runner, once per suite. Both now start
// together and the suite fails when either one fails, so the same two checks run
// in the time of the longer one.
//
// Playwright's output streams as it runs. The type checker's is held and printed
// once both have finished, so the two never interleave. Arguments after the config
// go to Playwright, as `npm run test:<suite> -- <spec files>` did before.
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const [tsconfig, config, ...forwarded] = process.argv.slice(2);
if (!tsconfig || !config) {
  console.error('usage: node scripts/e2e-suite.mjs <tsconfig> <playwright config> [playwright args...]');
  process.exit(2);
}

const children = new Set();
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    for (const child of children) child.kill(signal);
  });
}

function run(script, args, { capture }) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [script, ...args], {
      stdio: capture ? ['ignore', 'pipe', 'pipe'] : 'inherit',
    });
    children.add(child);
    let output = '';
    if (capture) {
      child.stdout.on('data', (chunk) => (output += chunk));
      child.stderr.on('data', (chunk) => (output += chunk));
    }
    child.on('error', (error) => {
      children.delete(child);
      resolve({ code: 1, output: `${output}${error.message}\n` });
    });
    child.on('close', (code) => {
      children.delete(child);
      resolve({ code: code ?? 1, output });
    });
  });
}

const [typecheck, suite] = await Promise.all([
  run(require.resolve('typescript/bin/tsc'), ['-p', tsconfig, '--noEmit'], { capture: true }),
  run(require.resolve('@playwright/test/cli'), ['test', '--config', config, ...forwarded], { capture: false }),
]);

if (typecheck.code === 0) {
  console.log(`tsc -p ${tsconfig}: no type errors`);
} else {
  console.error(`\ntsc -p ${tsconfig} failed:\n${typecheck.output}`);
}
process.exitCode = suite.code || typecheck.code;
