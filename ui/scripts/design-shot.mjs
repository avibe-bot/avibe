#!/usr/bin/env node
// Render design frames and save one PNG per frame and theme.
//
//   npm run design:shot                      every frame, every theme it declares
//   npm run design:shot -- sidebar/bots      frames whose id starts with the argument
//   npm run design:shot -- --theme dark --lang en --out /tmp/shots
//   --manifest <file>                        also merge a JSON index of the shots into <file>
//
// Exits 1 when a frame logs a console error or fails to render, so it doubles as `design:check`.
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { chromium } from '@playwright/test';
import { createServer } from 'vite';

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const option = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  if (index === -1) return fallback;
  const [value] = args.splice(index, 2).slice(1);
  return value;
};
const theme = option('theme', null);
const lang = option('lang', 'zh');
const out = resolve(option('out', join(uiRoot, 'design', '.shots')));
const manifestPath = option('manifest', null);
const prefixes = args;

const server = await createServer({ root: uiRoot, logLevel: 'error', server: { port: 0, strictPort: false } });
await server.listen();
const base = server.resolvedUrls.local[0];
const browser = await chromium.launch();
const page = await browser.newPage({ deviceScaleFactor: 2 });
const errors = [];
page.on('console', (message) => {
  if (message.type() === 'error') errors.push(message.text());
});
page.on('pageerror', (error) => errors.push(String(error)));

let failed = false;
const shots = [];
let all = [];
try {
  await page.goto(`${base}design.html?lang=${lang}`);
  await page.waitForFunction(() => window.__DESIGN__);
  all = await page.evaluate(() => window.__DESIGN__.frames);
  const selected = all.filter((f) => prefixes.length === 0 || prefixes.some((p) => f.id.startsWith(p)));
  if (selected.length === 0) throw new Error(`no frame matches ${prefixes.join(', ')}`);
  for (const frame of selected) {
    for (const t of theme ? [theme] : frame.themes) {
      errors.length = 0;
      const started = Date.now();
      await page.setViewportSize({ width: frame.width, height: frame.height });
      await page.goto(`${base}design.html?frame=${encodeURIComponent(frame.id)}&theme=${t}&lang=${lang}`);
      await page.waitForSelector('body[data-design-ready]', { timeout: 15_000 });
      if (frame.hover) {
        // Hover states open on a delay and may fetch; wait them out.
        await page.hover(frame.hover);
        await page.waitForTimeout(800);
        await page.evaluate(() => window.__DESIGN__.quiet());
      }
      const file = join(out, `${frame.id}.${t}.png`);
      await mkdir(dirname(file), { recursive: true });
      // The viewport is the frame, so popovers and dialogs portaled to <body> are captured too.
      const png = await page.screenshot({ path: file, animations: 'disabled', clip: { x: 0, y: 0, width: frame.width, height: frame.height } });
      const unfixtured = await page.evaluate(() => window.__DESIGN__.unfixtured());
      const result = { frame: frame.id, theme: t, file, ms: Date.now() - started, unfixtured, errors: [...errors] };
      if (errors.length) failed = true;
      shots.push({ ...frame, theme: t, png: file, hash: createHash('sha1').update(png).digest('hex').slice(0, 10) });
      console.log(JSON.stringify(result));
    }
  }
} catch (error) {
  failed = true;
  console.error(String(error));
} finally {
  await browser.close();
  await server.close();
}

if (manifestPath && shots.length) {
  // Merge: re-rendered shots replace their entries, frames that no longer exist drop out.
  const known = new Set(all.map((f) => f.id));
  let previous = [];
  try {
    previous = JSON.parse(await readFile(manifestPath, 'utf8')).shots ?? [];
  } catch {
    previous = [];
  }
  const key = (s) => `${s.id}.${s.theme}`;
  const fresh = new Map(
    shots.map((s) => [
      key(s),
      { id: s.id, file: s.file, title: s.title, status: s.status, theme: s.theme, lang, width: s.width, height: s.height, hash: s.hash, src: relative(dirname(out), s.png).split('\\').join('/') },
    ]),
  );
  const merged = [...previous.filter((s) => known.has(s.id) && !fresh.has(key(s))), ...fresh.values()].sort((a, b) => key(a).localeCompare(key(b)));
  await mkdir(dirname(manifestPath), { recursive: true });
  await writeFile(manifestPath, JSON.stringify({ generatedAt: new Date().toISOString(), lang, shots: merged }, null, 2));
}
process.exit(failed ? 1 : 0);
