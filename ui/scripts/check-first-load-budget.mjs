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
//
// home 600 -> 608 KiB (2026-11-20). Measured 600.4 KiB on master after Ask
// Vibey (#4035, +1.5 KiB: the desktop compose receiver AppShell installs so a
// macOS Services selection can reach the composer) and the App Lock setting
// (#4040, +0.9 KiB). With the W1 native windows (#4036: the App launch protocol
// and the native window close confirmation, needed before any App window
// opens) it is 603.3 KiB, and 603.5 KiB with the Show share entry (#4037). All
// of these are first-screen behaviour. Candidates to move behind an on-demand
// import next: the App launch protocol (src/apps/appLaunchProtocol.ts, with
// #4036) and the voice input stack (src/components/voice/, src/lib/voiceActivity.ts).
//
// home 608 -> 600 KiB (2026-11-20, same day). Two things the first screen never
// draws now load when first needed, and home measures 591.6 KiB: the Ask Vibey
// receiver is fetched only in a desktop shell (-1.0 KiB), and the new-project
// dialog with its folder browser loads on first open (-7.9 KiB). The 3.1 KiB
// named above for #4036 and #4037 fit under 600 with room left.
//
// home 600 -> 604 KiB (2026-11-22). Measured 600.1 KiB with the Devices pages
// (Settings · 设备 in place of Settings · Remote access). No code was added to
// the first screen: the pages load with Settings, and the one screen the entry
// guard can show loads on demand. The growth is copy: the locale chunk, which
// every first load takes whole, is 3.9 KiB larger (the sentences for claims,
// sign-out results, renaming and binding again, in place of the old page's).
// The candidate to win it back is the locale itself: one chunk per surface
// instead of one per language, so Settings' copy stops riding with the home.
//
// home 604 -> 608 KiB (2026-11-22, decided by the owner of merges). Temporary,
// until the locale is split by surface; then back within 600, at the latest
// by 3.2.9 RC1. 608 covers the growth brought by the Devices pages (#4147),
// #4166 and rc12. If master still crosses 608, whoever merges last resolves
// it; it is not raised again without him, and no sentence is deleted to fit.
// Splitting the locale by surface is its own task, not part of #4147.
//
// chat 900 -> 904 KiB (2026-11-24, decided by the owner of merges). Temporary,
// until the locale is split by surface; then back within 900, at the latest
// by 3.2.9 RC1. Master measured 899.9 KiB after the image preview's touch
// paging (#4225). #4163 adds 0.5 KiB to every first load (the intent a press
// on the notification switch carries, the Settings and access-blocked
// sentences, and the check that decides whether a repair can be on offer) and
// measures 900.4 KiB; the repair itself was moved off the first screen first
// (src/lib/webPushBindingRepair.ts loads on demand). It is not raised again
// without him.
const BUDGETS = {
  home: 608 * KiB,
  chat: 904 * KiB,
};

const dist = process.argv[2] ?? join(dirname(fileURLToPath(import.meta.url)), '..', 'dist');
const html = readFileSync(join(dist, 'index.html'), 'utf8');

// The first screen draws only what the server sends: a cross-origin stylesheet,
// script or preconnect would block or delay render on a third party the budget
// cannot measure (the type tokens in src/index.css name local/system fonts).
const crossOrigin = [...html.matchAll(/<(?:script|link)\b[^>]*\b(?:src|href)="((?:https?:)?\/\/[^"]+)"/g)].map((m) => m[1]);
if (crossOrigin.length) {
  console.error(`check-first-load-budget: index.html loads from another origin: ${crossOrigin.join(', ')}`);
  process.exit(1);
}

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
