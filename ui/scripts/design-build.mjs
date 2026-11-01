#!/usr/bin/env node
// Build the design canvas as static files: `npm run design:build -- --out <dir>`.
// Paths are relative, so the output works under any base, such as a Show Page's public/ folder.
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { build } from 'vite';

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const index = process.argv.indexOf('--out');
const outDir = resolve(index === -1 ? join(uiRoot, 'design', '.dist') : process.argv[index + 1]);

await build({
  root: uiRoot,
  base: './',
  logLevel: 'warn',
  publicDir: false,
  build: { outDir, emptyOutDir: true, rollupOptions: { input: join(uiRoot, 'design.html') } },
});
console.log(outDir);
