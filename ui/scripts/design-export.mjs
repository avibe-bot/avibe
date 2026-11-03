#!/usr/bin/env node
// Export design frames for a Show Page: the live canvas (real components, interactive),
// a screenshot of each frame, frames.json, and the <DesignFrame> component that embeds them.
//
//   npm run design:export -- sidebar/bots                    into this Agent Session's Show Page
//   npm run design:export -- sidebar/bots --show <workspace> into another Show Page workspace
//
// Then, in a page of that Show workspace:
//   import { DesignFrame } from "../design/DesignFrame"
//   <DesignFrame id="sidebar/bots/three" />
import { execFileSync } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readdir, rm, utimes } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const index = args.indexOf('--show');
const show = resolve(index === -1 ? JSON.parse(execFileSync('vibe', ['show', 'path'], { encoding: 'utf8' })).path : args.splice(index, 2)[1]);
const out = join(show, 'public', 'design');
const node = (script, extra) => execFileSync(process.execPath, [join(uiRoot, 'scripts', script), ...extra], { stdio: ['ignore', 'ignore', 'inherit'] });

// The Show Page's dev server serves public/ from an index its file watcher keeps: a file it saw
// no "add" event for answers 404. Building straight into public/design/canvas empties it first,
// so files that keep their names are deleted and re-created at once, and the watcher can merge the
// two into a change and drop them. So build elsewhere and sync: overwrite in place, delete only
// what is gone, then touch the new files, which makes a watcher that missed them add them.
async function files(dir) {
  const entries = await readdir(dir, { recursive: true, withFileTypes: true }).catch(() => []);
  return entries.filter((e) => e.isFile()).map((e) => relative(dir, join(e.parentPath, e.name)));
}
async function sync(from, to) {
  const before = new Set(await files(to));
  const next = await files(from);
  for (const file of next) {
    await mkdir(dirname(join(to, file)), { recursive: true });
    await copyFile(join(from, file), join(to, file));
  }
  const keep = new Set(next);
  for (const file of before) if (!keep.has(file)) await rm(join(to, file), { force: true });
  return next.filter((file) => !before.has(file));
}

const staging = await mkdtemp(join(tmpdir(), 'design-canvas-'));
node('design-build.mjs', ['--out', staging]);
const added = await sync(staging, join(out, 'canvas'));
await rm(staging, { recursive: true, force: true });
const existing = new Set(await files(out));
let failed = false;
try {
  node('design-shot.mjs', ['--out', join(out, 'shots'), '--manifest', join(out, 'frames.json'), ...args]);
} catch {
  failed = true;
}
await new Promise((resolve) => setTimeout(resolve, 500));
const now = new Date();
const created = [...added.map((file) => join('canvas', file)), ...(await files(out)).filter((file) => !existing.has(file))];
for (const file of new Set(created)) await utimes(join(out, file), now, now).catch(() => undefined);
await mkdir(join(show, 'src', 'design'), { recursive: true });
await copyFile(join(uiRoot, 'design', 'export', 'DesignFrame.tsx'), join(show, 'src', 'design', 'DesignFrame.tsx'));
console.log(`Exported to ${out}\nIn a page of ${show}:\n  import { DesignFrame } from "../design/DesignFrame"\n  <DesignFrame id="<frame id>" />`);
process.exit(failed ? 1 : 0);
