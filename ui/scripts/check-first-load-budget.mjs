#!/usr/bin/env node
// Holds the Web UI's first screen to a download budget (docs/plans/web-ui-first-load.md).
//
// Reads the built dist/index.html: the entry, its modulepreloads and stylesheet
// are what every first load fetches; the inline first-screen script names the
// locale chunk (the larger of the two counts) and what a conversation adds
// (scripts/firstScreenPreload.ts). Each file is measured gzip-compressed at the
// level the UI server sends it (vibe/ui_server.py, level 6), so the numbers are
// the bytes a phone downloads on a first visit, and again after each release
// changes them.
//
// A budget is a decision, not a ratchet: raise it in the same change that needs
// the room, and say why there; move code off the first screen first
// (src/routePages.ts, src/lib/lazyPage.ts).
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

const KiB = 1024;
// gzip bytes of JavaScript. Measured at 551 KiB (home) and 831 KiB (a
// conversation) when set; before the split, every first load took 1215 KiB.
const BUDGETS = {
  home: 600 * KiB,
  chat: 900 * KiB,
};

const dist = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', 'dist');
const html = readFileSync(join(dist, 'index.html'), 'utf8');

const attributeFiles = [...html.matchAll(/<(?:script|link)\b[^>]*\b(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1]);
const firstScreen = html.match(/var L=(\{.*?\}),C=(\[.*?\]),d=document/);
if (!firstScreen) {
  console.error('check-first-load-budget: the first-screen preload script is missing from index.html');
  process.exit(1);
}
const locales = JSON.parse(firstScreen[1]);
const chat = JSON.parse(firstScreen[2]);

const gzipped = new Map();
const size = (file) => {
  if (!gzipped.has(file)) gzipped.set(file, gzipSync(readFileSync(join(dist, file)), { level: 6 }).length);
  return gzipped.get(file);
};
const sum = (files) => files.reduce((total, file) => total + size(file), 0);
const js = (files) => [...new Set(files)].filter((file) => file.endsWith('.js'));

const largestLocale = Object.values(locales).reduce((a, b) => (sum(a) >= sum(b) ? a : b));
const routes = {
  home: js([...attributeFiles, ...largestLocale]),
  chat: js([...attributeFiles, ...largestLocale, ...chat]),
};
const css = sum(attributeFiles.filter((file) => file.endsWith('.css')));

let over = false;
for (const [route, files] of Object.entries(routes)) {
  const bytes = sum(files);
  const budget = BUDGETS[route];
  const verdict = bytes <= budget ? 'ok' : 'OVER';
  if (bytes > budget) over = true;
  console.log(`${route.padEnd(5)} ${(bytes / KiB).toFixed(1).padStart(7)} KiB gzip JS in ${files.length} files`
    + ` (budget ${budget / KiB} KiB) ${verdict}`);
}
console.log(`css   ${(css / KiB).toFixed(1).padStart(7)} KiB gzip`);
if (over) {
  console.error('\nThe first screen grew past its budget. Load what it does not draw on demand'
    + ' (src/routePages.ts, src/lib/lazyPage.ts), or raise the budget in'
    + ' scripts/check-first-load-budget.mjs with the reason.');
  process.exit(1);
}
