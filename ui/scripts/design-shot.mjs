#!/usr/bin/env node
// Render design frames and save one PNG per frame and theme.
//
//   npm run design:shot                      every frame, every theme it declares
//   npm run design:shot -- sidebar/bots      frames whose id starts with the argument
//   npm run design:shot -- --theme dark --lang en --out /tmp/shots
//   --manifest <file>                        also merge a JSON index of the shots into <file>
//   --jobs <n>                               frames shot at once (default: up to 4, one less than the CPUs)
//
// Exits 1 when a frame logs a console error or fails to render, so it doubles as `design:check`.
import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { availableParallelism } from 'node:os';
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
const jobs = Math.max(1, Number(option('jobs', String(Math.min(4, Math.max(1, availableParallelism() - 1))))));
const prefixes = args;

const server = await createServer({ root: uiRoot, logLevel: 'error', server: { port: 0, strictPort: false } });
await server.listen();
const base = server.resolvedUrls.local[0];
const browser = await chromium.launch();
const context = await browser.newContext({ deviceScaleFactor: 2 });

// Each frame gets a fresh page. One page navigated from frame to frame for the whole run
// occasionally stopped evaluating a frame's modules after a hundred or so navigations: the
// document loaded, every request finished, no error was logged, and the frame never rendered.
async function shoot(frame, t) {
  const errors = [];
  const page = await context.newPage();
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(String(error)));
  const started = Date.now();
  const failure = (reason) => ({ line: { frame: frame.id, theme: t, ms: Date.now() - started, errors: [reason, ...errors] } });
  try {
    await page.setViewportSize({ width: frame.width, height: frame.height });
    await page.goto(`${base}design.html?frame=${encodeURIComponent(frame.id)}&theme=${t}&lang=${lang}`);
    const ready = await page.waitForSelector('body[data-design-ready]', { timeout: 15_000 }).then(() => true, () => false);
    if (!ready) {
      const stage = await Promise.race([
        page.evaluate(() => document.body.dataset.designStage ?? 'load').catch(() => 'navigation'),
        new Promise((resolve) => setTimeout(() => resolve('unresponsive'), 2000)),
      ]);
      return failure(`not ready after 15s (stage: ${stage})`);
    }
    // Real pointer clicks, so components that open on pointerdown open, and a focus a click moves
    // draws no keyboard ring.
    for (const spec of frame.click ?? []) {
      // `{t:<key>}` is copy in the frame's language, resolved by the page.
      let target = spec;
      for (const [token, key] of spec.matchAll(/\{t:([^}]+)\}/g)) {
        target = target.replace(token, await page.evaluate((k) => window.__DESIGN__.t(k), key));
      }
      const clicked = await page.click(target, { timeout: 5_000 }).then(() => true, () => false);
      if (!clicked) return failure(`nothing to click for ${target}`);
      await page.waitForTimeout(300);
      await page.evaluate(() => window.__DESIGN__.quiet());
      // A click that starts a timed transition (a screen handoff) is waited out like a timed read.
      if (frame.wait) {
        await page.waitForTimeout(frame.wait);
        await page.evaluate(() => window.__DESIGN__.quiet());
      }
    }
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
    return {
      line: { frame: frame.id, theme: t, file, ms: Date.now() - started, unfixtured, errors },
      shot: { ...frame, theme: t, png: file, hash: createHash('sha1').update(png).digest('hex').slice(0, 10) },
    };
  } finally {
    await page.close();
  }
}

let failed = false;
const shots = [];
let all = [];
try {
  // The frame list, without rendering the canvas (every frame in an iframe).
  const list = await context.newPage();
  await list.goto(`${base}design.html?list&lang=${lang}`);
  await list.waitForFunction(() => window.__DESIGN__);
  all = await list.evaluate(() => window.__DESIGN__.frames);
  await list.close();
  const selected = all.filter((f) => prefixes.length === 0 || prefixes.some((p) => f.id.startsWith(p)));
  if (selected.length === 0) throw new Error(`no frame matches ${prefixes.join(', ')}`);
  const tasks = selected.flatMap((frame) => (theme ? [theme] : frame.themes).map((t) => [frame, t]));
  // Frames are shot side by side: most of a frame's time is waiting (reads, clicks, transitions).
  let next = 0;
  const worker = async () => {
    while (next < tasks.length) {
      const [frame, t] = tasks[next++];
      const { line, shot } = await shoot(frame, t);
      if (!shot || line.errors.length) failed = true;
      if (shot) shots.push(shot);
      // A frame that fails is named and the run goes on, so one run reports every failing frame.
      console.log(JSON.stringify(line));
    }
  };
  await Promise.all(Array.from({ length: Math.min(jobs, tasks.length) }, worker));
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
      { id: s.id, file: s.file, title: s.title, status: s.status, theme: s.theme, lang, width: s.width, height: s.height, interaction: Boolean(s.hover || s.click?.length), hash: s.hash, src: relative(dirname(out), s.png).split('\\').join('/') },
    ]),
  );
  const merged = [...previous.filter((s) => known.has(s.id) && !fresh.has(key(s))), ...fresh.values()].sort((a, b) => key(a).localeCompare(key(b)));
  await mkdir(dirname(manifestPath), { recursive: true });
  await writeFile(manifestPath, JSON.stringify({ generatedAt: new Date().toISOString(), lang, shots: merged }, null, 2));
}
process.exit(failed ? 1 : 0);
